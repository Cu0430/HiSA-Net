import warnings
from sklearn.exceptions import ConvergenceWarning 
import math
import torch.nn as nn
import torch.nn.functional as F
import torch
import os
from sklearn.cluster import KMeans
import numpy as np

os.environ['CUDA_LAUNCH_BLOCKING'] = '0'
#[128,4,16,16][128,1,64,64][128,4,16,16]
def Loss_intensity(ms, pan, image_fused):
    # 统一尺寸
    ms = F.interpolate(ms, size=(64, 64), mode='bilinear', align_corners=False)
    # 统一通道数
    pan = pan.expand(-1, 4, -1, -1) 

    assert (ms.size() == pan.size() == image_fused.size())
    ms_li = F.l1_loss(image_fused, ms)
    pan_li = F.l1_loss(image_fused, pan)
    li = ms_li + pan_li
    return li


class CorrelationCoefficient(nn.Module):
    def __init__(self):
        super(CorrelationCoefficient, self).__init__()

    def c_CC(self, A, B): #[128,256,4,4] [128,64,16,16]
        A_mean = torch.mean(A, dim=[2, 3], keepdim=True) 
        B_mean = torch.mean(B, dim=[2, 3], keepdim=True) 
        A_sub_mean = A - A_mean 
        B_sub_mean = B - B_mean 
        sim = torch.sum(torch.mul(A_sub_mean, B_sub_mean))
        A_sdev = torch.sqrt(torch.sum(torch.pow(A_sub_mean, 2)))
        B_sdev = torch.sqrt(torch.sum(torch.pow(B_sub_mean, 2)))
        out = sim / (A_sdev * B_sdev)
        return out

    def forward(self, A, B, Fusion=None):
        if Fusion is None:
            A_resized = F.interpolate(A, size=(B.shape[2], B.shape[3]), mode='bilinear', align_corners=False)
            CC = self.c_CC(A_resized, B)
        else:
            r_1 = self.c_CC(A, Fusion)
            r_2 = self.c_CC(B, Fusion)
            CC = (r_1 + r_2) / 2
        return CC

class L_Grad(nn.Module):
    def __init__(self):
        super(L_Grad, self).__init__()
        self.sobelconv = Sobelxy()
#[128,4,16,16][128,1,64,64][128,4,64,64]
    def forward(self, img1, img2, image_fused=None):
        if image_fused == None:
            image_1_Y = img1[:, :1, :, :]
            image_2_Y = img2[:, :1, :, :]
            gradient_1 = self.sobelconv(image_1_Y)
            gradient_2 = self.sobelconv(image_2_Y)
            Loss_gradient = F.l1_loss(gradient_1, gradient_2)
            return Loss_gradient
        else:
            image_1_Y = img1[:, :1, :, :] #[128,1,16,16]
            image_1_Y = F.interpolate(image_1_Y, size=(64, 64), mode='bilinear', align_corners=False)
            image_2_Y = img2[:, :1, :, :] #[128,1,64,64]
            image_fused_Y = image_fused[:, :1, :, :] #[128,1,64,64]
            
            gradient_1 = self.sobelconv(image_1_Y) #[128,1,64,64]
            gradient_2 = self.sobelconv(image_2_Y) #[128,1,64,64]
            gradient_fused = self.sobelconv(image_fused_Y) #[128,1,64,64]
                        
            gradient_joint = torch.max(gradient_1, gradient_2) 
            
            Loss_gradient = F.l1_loss(gradient_fused, gradient_joint)
            return Loss_gradient


class Sobelxy(nn.Module):
    def __init__(self):
        super(Sobelxy, self).__init__()
        kernelx = [[-1, 0, 1],
                   [-2, 0, 2],
                   [-1, 0, 1]]
        kernely = [[1, 2, 1],
                   [0, 0, 0],
                   [-1, -2, -1]]
        kernelx = torch.FloatTensor(kernelx).unsqueeze(0).unsqueeze(0)
        kernely = torch.FloatTensor(kernely).unsqueeze(0).unsqueeze(0)
        self.weightx = nn.Parameter(data=kernelx, requires_grad=False)
        self.weighty = nn.Parameter(data=kernely, requires_grad=False)

    def forward(self, x):
        sobelx = F.conv2d(x, self.weightx.to(x.device), padding=1)
        sobely = F.conv2d(x, self.weighty.to(x.device), padding=1)
        return torch.abs(sobelx) + torch.abs(sobely)
    
class IncrementalFalseNegativeDetection:
    def __init__(self, max_clusters=50, acceptance_rate=0.1):
        """
        误判负样本检测 (False Negative Detection)
        Args:
            num_clusters (int): K-Means 聚类簇数
            acceptance_rate (float): 伪标签接受率
        """
        self.max_clusters = max_clusters
        self.acceptance_rate = acceptance_rate
        self.current_clusters = 10  # 初始化较少的聚类数

    def update_clusters(self, epoch, total_epochs):
        # 动态增长聚类数（线性增长）
        self.current_clusters = min(
            self.max_clusters,
            int(10 + (self.max_clusters - 10) * (epoch / total_epochs))
        )

    def assign_pseudo_labels(self, ms_features, pan_features):
        """
        在两个模态（MS 和 PAN）上联合进行 K-Means 聚类，分配伪标签
        Args:
            ms_features (Tensor): MS 模态特征 (batch_size, embed_dim)
            pan_features (Tensor): PAN 模态特征 (batch_size, embed_dim)
        Returns:
            pseudo_labels_ms (Tensor): MS 模态伪标签
            pseudo_labels_pan (Tensor): PAN 模态伪标签
        """
        # 拼接两个模态的特征
        joint_features = torch.cat([ms_features, pan_features], dim=0)
        joint_features_np = joint_features.detach().cpu().numpy()
        
        # 如果样本数不够聚类数，返回默认伪标签
        if joint_features_np.shape[0] < self.current_clusters:
            print(f"[SKIP] 样本数({joint_features_np.shape[0]}) < 聚类数({self.current_clusters})，跳过聚类。")
            dummy = torch.zeros(ms_features.shape[0], dtype=torch.long, device=ms_features.device)
            centers_dummy = torch.zeros((self.current_clusters, ms_features.shape[1]), device=ms_features.device)
            return dummy.clone(), dummy.clone(), centers_dummy

        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("error", category=ConvergenceWarning)
                kmeans = KMeans(n_clusters=self.current_clusters, random_state=0, n_init="auto")
                cluster_ids = kmeans.fit_predict(joint_features_np)
                cluster_centers = kmeans.cluster_centers_
        except Exception as e:
            print(f"[WARN] 聚类失败，使用默认伪标签。原因: {e}")
            cluster_ids = np.zeros(joint_features_np.shape[0], dtype=np.int32)
            cluster_centers = np.zeros((self.current_clusters, ms_features.shape[1]))
        # K-Means 进行聚类
        #kmeans = KMeans(n_clusters=self.current_clusters, random_state=0)
        #cluster_ids = kmeans.fit_predict(joint_features_np)

        # 转换为张量
        pseudo_labels = torch.tensor(cluster_ids, dtype=torch.long, device=ms_features.device)
        cluster_centers = torch.tensor(cluster_centers, dtype=torch.float, device=ms_features.device)
        # 返回 MS 和 PAN 各自的伪标签
        return pseudo_labels[: ms_features.shape[0]], pseudo_labels[ms_features.shape[0]:], cluster_centers

    def update_acceptance_rate(self, epoch, total_epochs):
        """线性增加伪标签接受率"""
        self.acceptance_rate = min(1.0, epoch / total_epochs)

class ContrastiveLoss(nn.Module):
    def __init__(self, temperature=0.07, negative_weight=0.8, num_clusters=50, acceptance_rate=0.1):
        super().__init__()
        self.logit_scale = nn.Parameter(torch.ones([]))
        self.criterion = torch.nn.CrossEntropyLoss(reduction='none')
        self.temperature = temperature
        self.negative_w = negative_weight  # 负样本权重

        # 误判负样本检测
        self.false_negative_detector = IncrementalFalseNegativeDetection(max_clusters=num_clusters, acceptance_rate=acceptance_rate)

    def compute_loss(self, logits, mask):
        """计算对比损失"""
        softmax_logits = F.softmax(logits, dim=1)
        softmax_logits = torch.clamp(softmax_logits, min=1e-8)  # 防止出现0，确保数值稳定
        return -torch.log((softmax_logits * mask).sum(1) + 1e-8)


    def _get_positive_mask(self, batch_size):
        """获取正样本 mask"""
        diag = np.eye(batch_size)
        mask = torch.from_numpy((diag))
        mask = (1 - mask)
        return mask.cuda(non_blocking=True)

    def filter_false_negatives(self, negatives, ms_features, pan_features):
        """
        使用联合 K-Means 伪标签来过滤误判负样本
        """
        pseudo_labels_ms, pseudo_labels_pan, centers = self.false_negative_detector.assign_pseudo_labels(ms_features, pan_features)
        batch_size = ms_features.shape[0]

        # 构建跨模态的误判负样本掩码
        false_negative_mask_ms = torch.zeros((batch_size, batch_size), device=ms_features.device)
        false_negative_mask_pan = torch.zeros((batch_size, batch_size), device=pan_features.device)

        # 对 MS 模态计算伪标签置信度
        ms_sim = F.cosine_similarity(ms_features.unsqueeze(1), centers.unsqueeze(0), dim=2)  # (B, C)
        ms_confidence = ms_sim.max(dim=1)[0] / (ms_sim.sum(dim=1) + 1e-6)  # (B,)

        # 对 PAN 模态计算伪标签置信度
        pan_sim = F.cosine_similarity(pan_features.unsqueeze(1), centers.unsqueeze(0), dim=2)
        pan_confidence = pan_sim.max(dim=1)[0] / (pan_sim.sum(dim=1) + 1e-6)

        # 获取接受阈值（只保留前 top-k% 高置信度样本用于误判负样本掩码）
        topk_ms = int(batch_size * self.false_negative_detector.acceptance_rate)
        topk_pan = int(batch_size * self.false_negative_detector.acceptance_rate)

        _, top_idx_ms = torch.topk(ms_confidence, topk_ms)
        _, top_idx_pan = torch.topk(pan_confidence, topk_pan)

        for i in top_idx_ms:
            false_negative_mask_ms[i] = (pseudo_labels_ms == pseudo_labels_ms[i]).float()
        for i in top_idx_pan:
            # 过滤误判的负样本
            false_negative_mask_pan[i] = (pseudo_labels_pan == pseudo_labels_pan[i]).float()

        negatives_ms = negatives * (1 - false_negative_mask_ms)
        negatives_pan = negatives * (1 - false_negative_mask_pan)

        return negatives_ms, negatives_pan

    def forward(self, ms_features, pan_features):
        """
        计算对比损失
        Args:
            ms_features (Tensor): MS 模态特征 (batch_size, embed_dim)
            pan_features (Tensor): PAN 模态特征 (batch_size, embed_dim)
        Returns:
            loss (Tensor): 计算出的对比损失
        """
        batch_size = ms_features.shape[0]

        # 归一化特征
        ms_features = F.normalize(ms_features, dim=1)
        pan_features = F.normalize(pan_features, dim=1)
        #print(torch.isnan(ms_features).sum(), torch.isnan(pan_features).sum())
        
        # 计算跨模态对齐的 logits
        logits_per_ms = ms_features @ pan_features.t()
        logits_per_pan = pan_features @ ms_features.t()
        #print(torch.isnan(logits_per_ms).sum(), torch.isnan(logits_per_pan).sum())
        
        # 计算模态内的 logits
        logits_clstr_ms = ms_features @ ms_features.t()
        logits_clstr_pan = pan_features @ pan_features.t()
        #print(torch.isnan(logits_clstr_ms).sum(), torch.isnan(logits_clstr_pan).sum())
        # 温度缩放
        logits_per_ms /= self.temperature
        logits_per_pan /= self.temperature
        logits_clstr_ms /= self.temperature
        logits_clstr_pan /= self.temperature
        #print(torch.isnan(logits_per_ms).sum(), torch.isnan(logits_per_pan).sum())
        # 计算正样本 mask
        positive_mask = self._get_positive_mask(batch_size)
        negatives_ms = logits_clstr_ms * positive_mask
        negatives_pan = logits_clstr_pan * positive_mask
        #print(torch.isnan(negatives_ms).sum(), torch.isnan(negatives_pan).sum())

        # **跨模态误判负样本过滤**
        #negatives_ms, negatives_pan = self.filter_false_negatives(negatives_ms, ms_features, pan_features)

        # 构造最终的 logits
        ms_logits = torch.cat([logits_per_ms, self.negative_w * negatives_ms], dim=1)
        pan_logits = torch.cat([logits_per_pan, self.negative_w * negatives_pan], dim=1)

        mask_ms = torch.eye(batch_size, device=ms_features.device)+ 1e-8 
        mask_pan = torch.eye(batch_size, device=pan_features.device)+ 1e-8 

        mask_neg_m = torch.zeros_like(negatives_ms)
        mask_neg_p = torch.zeros_like(negatives_pan)
        mask_m = torch.cat([mask_ms, mask_neg_m], dim=1)
        mask_p = torch.cat([mask_pan, mask_neg_p], dim=1)

        loss_m = self.compute_loss(ms_logits, mask_m)
        loss_p = self.compute_loss(pan_logits, mask_p)

        return (loss_m.mean() + loss_p.mean()) / 2
    



