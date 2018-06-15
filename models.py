import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from config import config

D = config.connector_dim
Nh = config.num_heads
Dword = config.glove_dim
Dchar = config.char_dim
batch_size = config.batch_size
dropout = config.dropout
dropout_char = config.dropout_char

Dk = D // Nh
Dv = D // Nh
D_cq_att = D * 4
Lc = config.para_limit
Lq = config.ques_limit


class PosEncoder(nn.Module):
    def __init__(self, length):
        super().__init__()
        freqs = torch.Tensor([10000 ** (-i / D) if i % 2 == 0 else -10000 ** (-(i - 1) / D) for i in
                              range(D)]).unsqueeze(dim=1)
        phases = torch.Tensor([0 if i % 2 == 0 else math.pi / 2 for i in range(D)]).unsqueeze(dim=1)
        pos = torch.arange(length).repeat(D, 1)
        self.pos_encoding = nn.Parameter(torch.sin(torch.add(torch.mul(pos, freqs), phases)).data, requires_grad=False)

    def forward(self, x):
        x.add_(self.pos_encoding)
        return x


class DepthwiseSeparableConv(nn.Module):
    def __init__(self, in_ch, out_ch, k, dim=1, bias=False):
        super().__init__()
        if dim == 1:
            self.depthwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=in_ch, kernel_size=k, groups=in_ch,
                                            padding=k // 2, bias=bias)
            self.pointwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=out_ch, kernel_size=1, padding=0, bias=bias)
        elif dim == 2:
            self.depthwise_conv = nn.Conv2d(in_channels=in_ch, out_channels=in_ch, kernel_size=k, groups=in_ch,
                                            padding=k // 2, bias=bias)
            self.pointwise_conv = nn.Conv2d(in_channels=in_ch, out_channels=out_ch, kernel_size=1, padding=0, bias=bias)
        else:
            raise Exception("Wrong dimension for Depthwise Separable Convolution!")

    def forward(self, x):
        return self.pointwise_conv(self.depthwise_conv(x))


class Highway(nn.Module):
    def __init__(self, layer_num: int, size=D):
        super().__init__()
        self.n = layer_num
        self.linear = nn.ModuleList([nn.Linear(size, size) for _ in range(self.n)])
        self.gate = nn.ModuleList([nn.Linear(size, size) for _ in range(self.n)])

    def forward(self, x):
        x = x.transpose(1, 2)
        for i in range(self.n):
            gate = F.sigmoid(self.gate[i](x))
            nonlinear = F.relu(self.linear[i](x), inplace=True)
            x = gate * nonlinear + (1 - gate) * x
        x = x.transpose(1, 2)
        return x


class SelfAttention(nn.Module):
    def __init__(self):
        super().__init__()
        Wo = torch.empty(batch_size, Dv * Nh, D)
        Wqs = [torch.empty(batch_size, Dk, D) for _ in range(Nh)]
        Wks = [torch.empty(batch_size, Dk, D) for _ in range(Nh)]
        Wvs = [torch.empty(batch_size, Dv, D) for _ in range(Nh)]
        nn.init.xavier_normal_(Wo)
        for i in range(Nh):
            nn.init.xavier_normal_(Wqs[i])
            nn.init.xavier_normal_(Wks[i])
            nn.init.xavier_normal_(Wvs[i])
        self.Wo = nn.Parameter(Wo.data)
        self.Wqs = nn.ParameterList([nn.Parameter(X.data) for X in Wqs])
        self.Wks = nn.ParameterList([nn.Parameter(X.data) for X in Wks])
        self.Wvs = nn.ParameterList([nn.Parameter(X.data) for X in Wvs])

    def forward(self, x: torch.Tensor):
        WQs, WKs, WVs = [], [], []
        sqrt_dk_inv = 1/math.sqrt(Dk)
        for i in range(Nh):
            WQs.append(torch.bmm(self.Wqs[i], x))
            WKs.append(torch.bmm(self.Wks[i], x))
            WVs.append(torch.bmm(self.Wvs[i], x))
        heads = []
        for i in range(Nh):
            out = torch.bmm(WQs[i].transpose(1, 2), WKs[i])
            out = torch.mul(out, sqrt_dk_inv)
            # not sure... I think `dim` should be 1 since it weighted each column of `WVs[i]`
            out = F.softmax(out, dim=1)
            headi = torch.bmm(WVs[i], out)
            WVs[i], WKs[i], WQs[i] = None, None, None
            heads.append(headi)
        head = torch.cat(heads, dim=1)
        out = torch.bmm(self.Wo, head)
        return out


class Embedding(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = DepthwiseSeparableConv(Dchar, D, 5, dim=2, bias=True)
        self.conv1d = DepthwiseSeparableConv(Dword + D, D, 5, bias=True)
        self.high = Highway(2)

    def forward(self, ch_emb, wd_emb):
        ch_emb = ch_emb.permute(0, 3, 1, 2)
        # ch_emb = F.dropout(ch_emb, p=dropout_char, training=self.training)
        ch_emb = self.conv2d(ch_emb)
        ch_emb = F.relu(ch_emb, inplace=True)
        ch_emb, _ = torch.max(ch_emb, dim=3)
        ch_emb = ch_emb.squeeze()
        # wd_emb = F.dropout(wd_emb, p=dropout, training=self.training)
        wd_emb = wd_emb.transpose(1, 2)
        emb = torch.cat([ch_emb, wd_emb], dim=1)
        emb = self.conv1d(emb)
        emb = self.high(emb)
        return emb


class EncoderBlock(nn.Module):
    def __init__(self, conv_num: int, ch_num: int, k: int, length: int):
        super().__init__()
        self.convs = nn.ModuleList([DepthwiseSeparableConv(ch_num, ch_num, k) for _ in range(conv_num)])
        self.self_att = SelfAttention()
        W = torch.empty(batch_size, ch_num, ch_num)
        nn.init.xavier_normal_(W)
        self.W = nn.Parameter(W.data)
        self.pos = PosEncoder(length)
        self.norm = nn.LayerNorm([D, length])

    def forward(self, x):
        out = self.pos(x)
        res = out
        for i, conv in enumerate(self.convs):
            # out = self.norm(out)
            out = conv(out)
            out = F.relu(out, inplace=True)
            out.add_(res)
            # if (i + 1) % 2 == 0:
            #     out = F.dropout(out, p=dropout, training=self.training)
            res = out
            # out = self.norm(out)
        out = self.self_att(out)
        out.add_(res)
        out = F.dropout(out, p=dropout, training=self.training)
        res = out
        # out = self.norm(out)
        out = torch.bmm(self.W, out)
        out = F.relu(out, inplace=True)
        out = out + res
        # out = F.dropout(out, p=dropout, training=self.training)
        return out


class CQAttention(nn.Module):
    def __init__(self):
        super().__init__()
        W = torch.empty(batch_size * Lc, 1, D * 3)
        nn.init.xavier_normal_(W)
        self.W = nn.Parameter(W.data)

    def forward(self, C, Q):
        ss = []
        C = C.permute(0, 2, 1)
        Q = Q.permute(0, 2, 1)
        for i in range(Lq):
            q = Q[:, i, :].unsqueeze(1)
            QCi = torch.mul(q, C)
            Qi = q.expand(batch_size, Lc, D)
            Xi = torch.cat([Qi, C, QCi], dim=2).reshape(batch_size * Lc, D * 3).unsqueeze(2)
            Si = torch.bmm(self.W, Xi).reshape(batch_size, Lc, 1)
            ss.append(Si)
        S = torch.cat(ss, dim=2)
        S1 = F.softmax(S, dim=2)
        S2 = F.softmax(S, dim=1)
        A = torch.bmm(S1, Q)
        B = torch.bmm(torch.bmm(S1, S2.transpose(1, 2)), C)
        out = torch.cat([C, A, torch.mul(C, A), torch.mul(C, B)], dim=2).permute(0, 2, 1)
        # out = F.dropout(out, p=dropout, training=self.training)
        return out


class Pointer(nn.Module):
    def __init__(self):
        super().__init__()
        W1 = torch.empty(batch_size, 1, D * 2)
        W2 = torch.empty(batch_size, 1, D * 2)
        nn.init.xavier_normal_(W1)
        nn.init.xavier_normal_(W2)
        self.W1 = nn.Parameter(W1.data)
        self.W2 = nn.Parameter(W2.data)

    def forward(self, M1, M2, M3):
        X1 = torch.cat([M1, M2], dim=1)
        X2 = torch.cat([M1, M3], dim=1)
        Y1 = torch.bmm(self.W1, X1).squeeze()
        Y2 = torch.bmm(self.W2, X2).squeeze()
        return Y1, Y2


class QANet(nn.Module):
    def __init__(self, word_mat, char_mat):
        super().__init__()
        self.char_emb = nn.Embedding.from_pretrained(torch.Tensor(char_mat), freeze=(not config.pretrained_char))
        self.word_emb = nn.Embedding.from_pretrained(torch.Tensor(word_mat))
        self.emb = Embedding()
        self.c_emb_enc = EncoderBlock(conv_num=4, ch_num=D, k=7, length=Lc)
        self.q_emb_enc = EncoderBlock(conv_num=4, ch_num=D, k=7, length=Lq)
        self.cq_att = CQAttention()
        self.cq_resizer = DepthwiseSeparableConv(D * 4, D, 5)
        enc_blk = EncoderBlock(conv_num=2, ch_num=D, k=5, length=Lc)
        self.model_enc_blks = nn.Sequential(*([enc_blk] * 7))
        self.out = Pointer()

    def forward(self, Cwid, Ccid, Qwid, Qcid):
        Cw, Cc = self.word_emb(Cwid), self.char_emb(Ccid)
        Qw, Qc = self.word_emb(Qwid), self.char_emb(Qcid)
        C, Q = self.emb(Cc, Cw), self.emb(Qc, Qw)
        Ce = self.c_emb_enc(C)
        Qe = self.q_emb_enc(Q)
        X = self.cq_att(Ce, Qe)
        M0 = self.cq_resizer(X)
        #M1 = self.model_enc_blks(M0)
        #M2 = self.model_enc_blks(M1)
        #M3 = self.model_enc_blks(M2)
        p1, p2 = self.out(M0, M0, M0)
        return p1, p2
