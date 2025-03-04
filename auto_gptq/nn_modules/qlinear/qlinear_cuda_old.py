import math
from logging import getLogger

import numpy as np
import torch
import torch.nn as nn
import transformers

logger = getLogger(__name__)

try:
    import autogptq_cuda

    _autogptq_cuda_available = True
except ImportError:
    logger.warning('CUDA extension not installed.')
    _autogptq_cuda_available = False


class QuantLinear(nn.Module):
    def __init__(
        self,
        bits,
        group_size,
        infeatures,
        outfeatures,
        bias,
        use_cuda_fp16=True,
        kernel_switch_threshold=128,
        trainable=False
    ):

        super().__init__()
        if bits not in [2, 3, 4, 8]:
            raise NotImplementedError("Only 2,3,4,8 bits are supported.")
        if trainable:
            raise NotImplementedError("QuantLinear with cuda backend not support trainable mode yet.")
        self.infeatures = infeatures
        self.outfeatures = outfeatures
        self.bits = bits
        self.group_size = group_size if group_size != -1 else infeatures
        self.maxq = 2 ** self.bits - 1

        self.register_buffer(
            'qweight',
            torch.zeros((infeatures // 32 * self.bits, outfeatures), dtype=torch.int32)
        )
        self.register_buffer(
            'qzeros',
            torch.zeros((math.ceil(infeatures / self.group_size), outfeatures // 32 * self.bits), dtype=torch.int32)
        )
        self.register_buffer(
            'scales',
            torch.zeros((math.ceil(infeatures / self.group_size), outfeatures), dtype=torch.float16)
        )
        self.register_buffer(
            'g_idx',
            torch.tensor([i // self.group_size for i in range(infeatures)], dtype=torch.int32)
        )

        if bias:
            self.register_buffer('bias', torch.zeros((outfeatures), dtype=torch.float16))
        else:
            self.bias = None
        self.half_indim = self.infeatures // 2

        self.use_cuda_fp16 = use_cuda_fp16 if bits != 8 else False
        
        # is performed by unpacking the weights and using torch.matmul
        if self.bits in [2, 4, 8]:
            self.wf = torch.tensor(list(range(0, 32, self.bits)), dtype=torch.int32).unsqueeze(0)
        elif self.bits == 3:
            self.wf = torch.tensor(
                [
                    [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 0],
                    [0, 1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31],
                    [0, 2, 5, 8, 11, 14, 17, 20, 23, 26, 29, 0],
                ],
                dtype=torch.int32
            ).reshape(1, 3, 12)

        self.kernel_switch_threshold = kernel_switch_threshold
        self.autogptq_cuda_available = _autogptq_cuda_available
        if infeatures % 256 != 0 or outfeatures % 256 != 0:
            self.autogptq_cuda_available = False

        self.trainable = trainable

    def pack(self, linear, scales, zeros, g_idx):
        scales = scales.t().contiguous()
        zeros = zeros.t().contiguous()
        scale_zeros = zeros * scales
        self.scales = scales.clone().half()
        if linear.bias is not None:
            self.bias = linear.bias.clone().half()

        intweight = []
        for idx in range(self.infeatures):
            g_idx = idx // self.group_size
            intweight.append(
                torch.round(
                    (linear.weight.data[:, idx] + scale_zeros[g_idx]) / self.scales[g_idx]
                ).to(torch.int)[:, None]
            )
        intweight = torch.cat(intweight, dim=1)
        intweight = intweight.t().contiguous()
        intweight = intweight.numpy().astype(np.uint32)

        i = 0
        row = 0
        qweight = np.zeros(
            (intweight.shape[0] // 32 * self.bits, intweight.shape[1]), dtype=np.uint32
        )
        while row < qweight.shape[0]:
            if self.bits in [2, 4, 8]:
                for j in range(i, i + (32 // self.bits)):
                    qweight[row] |= intweight[j] << (self.bits * (j - i))
                i += 32 // self.bits
                row += 1
            elif self.bits == 3:
                for j in range(i, i + 10):
                    qweight[row] |= intweight[j] << (3 * (j - i))
                i += 10
                qweight[row] |= intweight[i] << 30
                row += 1
                qweight[row] |= (intweight[i] >> 2) & 1
                i += 1
                for j in range(i, i + 10):
                    qweight[row] |= intweight[j] << (3 * (j - i) + 1)
                i += 10
                qweight[row] |= intweight[i] << 31
                row += 1
                qweight[row] |= (intweight[i] >> 1) & 0x3
                i += 1
                for j in range(i, i + 10):
                    qweight[row] |= intweight[j] << (3 * (j - i) + 2)
                i += 10
                row += 1
            else:
                raise NotImplementedError("Only 2,3,4,8 bits are supported.")

        qweight = qweight.astype(np.int32)
        self.qweight = torch.from_numpy(qweight)

        zeros -= 1
        zeros = zeros.numpy().astype(np.uint32)
        qzeros = np.zeros((zeros.shape[0], zeros.shape[1] // 32 * self.bits), dtype=np.uint32)
        i = 0
        col = 0
        while col < qzeros.shape[1]:
            if self.bits in [2, 4, 8]:
                for j in range(i, i + (32 // self.bits)):
                    qzeros[:, col] |= zeros[:, j] << (self.bits * (j - i))
                i += 32 // self.bits
                col += 1
            elif self.bits == 3:
                for j in range(i, i + 10):
                    qzeros[:, col] |= zeros[:, j] << (3 * (j - i))
                i += 10
                qzeros[:, col] |= zeros[:, i] << 30
                col += 1
                qzeros[:, col] |= (zeros[:, i] >> 2) & 1
                i += 1
                for j in range(i, i + 10):
                    qzeros[:, col] |= zeros[:, j] << (3 * (j - i) + 1)
                i += 10
                qzeros[:, col] |= zeros[:, i] << 31
                col += 1
                qzeros[:, col] |= (zeros[:, i] >> 1) & 0x3
                i += 1
                for j in range(i, i + 10):
                    qzeros[:, col] |= zeros[:, j] << (3 * (j - i) + 2)
                i += 10
                col += 1
            else:
                raise NotImplementedError("Only 2,3,4,8 bits are supported.")

        qzeros = qzeros.astype(np.int32)
        self.qzeros = torch.from_numpy(qzeros)

    def forward(self, x):
        out_shape = x.shape[:-1] + (self.outfeatures,)
        x = x.reshape(-1, x.shape[-1])
        if self.autogptq_cuda_available is True and (
            self.kernel_switch_threshold is False or x.shape[0] < self.kernel_switch_threshold
        ):
            out = torch.zeros(x.shape[0], out_shape[-1], dtype=torch.float, device=x.device)
            if self.use_cuda_fp16:
                x = x.half()
                if self.bits == 2:
                    autogptq_cuda.vecquant2matmul_faster_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size, self.half_indim)
                elif self.bits == 3:
                    autogptq_cuda.vecquant3matmul_faster_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size, self.half_indim)
                elif self.bits == 4:
                    autogptq_cuda.vecquant4matmul_faster_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size, self.half_indim)

                else:
                    raise NotImplementedError("Only 2,3,4 bits are supported.")
            else:
                x = x.float()
                if self.bits == 2:
                    autogptq_cuda.vecquant2matmul_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size)
                elif self.bits == 3:
                    autogptq_cuda.vecquant3matmul_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size)
                elif self.bits == 4:
                    autogptq_cuda.vecquant4matmul_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size)
                elif self.bits == 8:
                    autogptq_cuda.vecquant8matmul_old(x, self.qweight, out, self.scales.float(), self.qzeros, self.group_size)
                else:
                    raise NotImplementedError("Only 2,3,4,8 bits are supported.")
        else:
            if self.wf.device != self.qzeros.device:
               self.wf = self.wf.to(self.qzeros.device)
                
            if self.bits in [2,4,8]:
               zeros = torch.bitwise_right_shift(torch.unsqueeze(self.qzeros, 2).expand(-1, -1, 32 // self.bits), self.wf.unsqueeze(0)).to(torch.int16 if self.bits == 8 else torch.int8)
               torch.bitwise_and(zeros, (2 ** self.bits) - 1, out=zeros)
                   
               zeros = zeros + 1
               zeros = zeros.reshape(-1, 1, zeros.shape[1] * zeros.shape[2])
   
               scales = self.scales
               scales = scales.reshape(-1, 1, scales.shape[-1])
                
               weight = torch.bitwise_right_shift(torch.unsqueeze(self.qweight, 1).expand(-1, 32 // self.bits, -1), self.wf.unsqueeze(-1)).to(torch.int16 if self.bits == 8 else torch.int8)
               torch.bitwise_and(weight,(2 ** self.bits) - 1, out=weight)
               weight = weight.reshape(-1, self.group_size, weight.shape[2])
            elif self.bits == 3:
               zeros = self.qzeros.reshape(self.qzeros.shape[0], self.qzeros.shape[1]//3, 3, 1).expand(-1, -1, -1, 12)
               zeros = (zeros >> self.wf.unsqueeze(0))
               zeros[:,:,0,10] = (zeros[:,:,0,10]&0x3) | ((zeros[:,:,1,0] << 2)&0x4)
               zeros[:,:,1,11] = (zeros[:,:,1,11]&0x1) | ((zeros[:,:,2,0] << 1)&0x6)
               zeros = zeros & 0x7
               zeros = torch.cat([zeros[:,:,0,:11], zeros[:,:,1,1:12], zeros[:,:,2,1:11]], dim=2)
                
               zeros = zeros + 1
               zeros = zeros.reshape(-1, 1, zeros.shape[1] * zeros.shape[2]) 
               
               scales = self.scales
               scales = scales.reshape(-1, 1, scales.shape[-1])
                
               weight = self.qweight.reshape(self.qweight.shape[0]//3, 3, 1, self.qweight.shape[1]).expand(-1, -1, 12, -1)
               weight = (weight >> self.wf.unsqueeze(-1))&0x7
               weight[:,0,10] = (weight[:,0,10]&0x3) | ((weight[:,1,0] << 2)&0x4)
               weight[:,1,11] = (weight[:,1,11]&0x1) | ((weight[:,2,0] << 1)&0x6)
               weight = weight & 0x7
               weight = torch.cat([weight[:,0,:11], weight[:,1,1:12], weight[:,2,1:11]], dim=1)
               weight = weight.reshape(-1, self.group_size, weight.shape[2])
            else:
               raise NotImplementedError("Only 2,3,4,8 bits are supported.")
            weight = (scales * (weight - zeros))
            weight = weight.reshape(weight.shape[0] * weight.shape[1], weight.shape[2])

            out = torch.matmul(x.half(), weight)
        out = out.half().reshape(out_shape)
        out = out + self.bias if self.bias is not None else out
        return out


__all__ = ["QuantLinear"]
