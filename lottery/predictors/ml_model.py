# -*- coding: utf-8 -*-
"""4.4 机器学习模型 ml_model.py (可选)
用历史期特征(和值/奇偶/区间/遗漏等)预测下期号码.
算法: XGBoost (multi-label: 每号码一个二分类器, 概率取Top6).
3495期对ML偏少, 易过拟合 — 必须靠 backtest 说话, 跑不出优势就停用.
"""
import numpy as np
from xgboost import XGBClassifier

from features import combo_features, ac_value


def build_features(hist_reds):
    """每期的组合特征向量: [和值,跨度,奇数,大数,连号,AC,一区,二区,三区]"""
    X = []
    for r in hist_reds:
        cf = combo_features(r)
        X.append([
            cf["和值"], cf["跨度"], cf["奇数"], cf["大数"], cf["连号"], cf["AC值"],
            cf["一区"], cf["二区"], cf["三区"],
        ])
    return np.array(X, dtype=float)


def build_labels(hist_reds):
    """标签: 下期号码 one-hot 33维"""
    Y = np.zeros((len(hist_reds) - 1, 33), dtype=int)
    for i in range(len(hist_reds) - 1):
        for x in hist_reds[i + 1]:
            Y[i, x - 1] = 1
    return Y


class MLModel:
    def __init__(self):
        self.models = None

    def fit(self, hist_reds):
        """hist_reds: 历史红球. 用 前N-1期特征 -> 第N期号码"""
        X = build_features(hist_reds[:-1])
        Y = build_labels(hist_reds)
        self.models = []
        for j in range(33):
            m = XGBClassifier(
                n_estimators=80, max_depth=3, learning_rate=0.1,
                subsample=0.8, colsample_bytree=0.8,
                eval_metric="logloss", verbosity=0, n_jobs=2,
            )
            m.fit(X, Y[:, j])
            self.models.append(m)
        return self

    def predict_proba(self, last_reds):
        """输入最后一期红球, 输出每个号码的下期出现概率"""
        X = build_features([last_reds])
        return np.array([m.predict_proba(X)[0, 1] for m in self.models])

    def predict(self, hist_reds):
        probs = self.predict_proba(hist_reds[-1])
        order = np.argsort(-probs)
        return (order[:6] + 1).tolist(), probs
