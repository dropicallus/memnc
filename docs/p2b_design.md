# Parametric Memory Vision: Design Document

## 1. Overview

This document outlines the design for a parametric memory approach focused on growing LoRA adapters as data volume increases. The goal is to explore whether scaling adapter capacity with dataset size yields efficient, generalizable models without full fine-tuning.

## 2. Core Approaches Compared

### 2.1 Growing-LoRA (MoRAM / CoDyRA Line)

The Growing-LoRA paradigm follows the MoRAM (Memory-of-Rank) and CoDyRA (Code-based Dynamic Rank Adaptation) research line. The key idea is:

- Start with a small base model frozen in place.
- Attach a LoRA adapter whose rank is intentionally chosen to be larger than typical practice, but still well below the full weight matrix dimension.
- As more training data becomes available, the effective rank of the adapter grows either by concatenation or by retraining with increased capacity.
- The adapter acts as a form of parametric memory: it stores task-relevant knowledge without modifying the base weights.

Advantages:
- Memory-efficient compared to full fine-tuning.
- Rank scaling is explicit and interpretable.
- Compatible with modular adapter composition.

### 2.2 RAG (Retrieval-Augmented Generation)

RAG approaches augment generation by retrieving external knowledge at inference time rather than encoding it into parameters. In the context of this design:

- The base model remains unchanged.
- Knowledge is stored in an external index or database.
- Retrieval happens during decoding to condition the output.

Comparison points:
- RAG requires an external retrieval system and indexing infrastructure.
- Growing-LoRA encodes knowledge directly into adapter weights, removing the need for a separate retriever at inference.
- Growing-LoRA may generalize better on distribution-shifted inputs where retrieval fails to find relevant examples.

## 3. Model Architecture Hypothesis

### 3.1 Small Base Model + Larger LoRA Rank

The working architecture hypothesis is:

- Use a relatively small base model (e.g., a few billion parameters).
- Attach a single LoRA adapter per task or domain.
- Choose the LoRA rank to be larger than standard practice (e.g., r=64, 128, or higher depending on data volume).
- Scale the rank proportionally with available training data: more data → higher rank.

Rationale:
- A small base model is cheap to maintain and deploy.
- The adapter carries the task-specific knowledge, acting as parametric memory.
- Larger ranks allow the adapter to capture richer representations without full fine-tuning cost.

### 3.2 Single-LoRA Advantage (Working Hypothesis)

The working hypothesis is that a single LoRA adapter per task/domain outperforms or matches multiple smaller adapters in terms of:

- Generalization on held-out data.
- Inference efficiency (fewer modules to load and apply).
- Stability during training (less interaction between competing adapters).

This hypothesis will be tested empirically in the experiment plan below.

## 4. Compression via Fisher-Merge

### 4.1 Fisher Information-Based Merge

After training a LoRA adapter on a given task, compression may be needed if the rank grows too large for deployment. The Fisher-Merge approach using mergekit provides:

- Compute the Fisher information matrix (or diagonal approximation) for the adapter weights.
- Identify weight dimensions that contribute least to the task-specific loss.
- Merge or prune low-importance dimensions while preserving high-importance ones.
- Use mergekit as the tooling framework for performing structured merges across adapters.

### 4.2 Compression Step in Pipeline

The compression step occurs after initial training and before deployment:
1. Train LoRA adapter to convergence on the task dataset.
2. Compute Fisher importance scores per weight dimension.
3. Apply mergekit to prune or compress low-importance dimensions.
4. Validate compressed adapter against the original uncompressed version.

This step ensures that the final deployed model is both accurate and memory-efficient.

## 5. Overfitting Metric

### 5.1 Train/Valid Loss Divergence

The primary overfitting metric for this design is the divergence between training loss and validation loss:

- Monitor both losses per epoch during training.
- Compute the gap: `divergence = train_loss - valid_loss` (or absolute difference).
- A growing positive divergence indicates overfitting to the training set.
- Set a threshold on this divergence to trigger early stopping or compression.

### 5.2 Secondary Metrics

- Validation perplexity or accuracy on held-out data.
- Adapter weight norm growth as a proxy for capacity utilization.
- Inference latency and memory footprint of the deployed adapter.

## 6. Experiment Plan

Three experiments are proposed to test the LoRA-rank scaling hypothesis.

### 6.1 Experiment 1: Rank Scaling on Fixed Data Volume

**Objective:** Determine how increasing LoRA rank affects performance when data volume is held constant.

**Setup:**
- Use a fixed dataset of moderate size (e.g., 50K samples).
- Train single LoRA adapters with ranks r=8, 16, 32, 64, 128.
- Hold all other hyperparameters constant.

**Success Criteria:**
- Validation loss decreases monotonically up to a point as rank increases.
- Beyond a certain rank (e.g., r=128), validation loss plateaus or worsens due to overfitting.
- The train/valid loss divergence remains below the set threshold for ranks up to the plateau point.

**Outcome:** Identify the optimal rank for this data volume and establish the overfitting boundary.

### 6.2 Experiment 2: Rank Scaling with Increasing Data Volume

**Objective:** Test whether higher ranks are justified when more training data is available.

**Setup:**
- Use datasets of increasing size: 10K, 50K, 200K, 1M samples.
- For each dataset size, train LoRA adapters with ranks scaled proportionally (e.g., r=16 for 10K, r=32 for 50K, r=64 for 200K, r=128 for 1M).
- Compare against a fixed-rank baseline (e.g., r=32 across all data sizes).

**Success Criteria:**
- Scaled-rank adapters achieve lower validation loss than the fixed-rank baseline on larger datasets.
- The train/valid loss divergence does not exceed the threshold for scaled-rank configurations.
- Compression via Fisher-Merge preserves at least 95% of the uncompressed adapter performance.

**Outcome:** Confirm that rank scaling with data volume yields generalizable models and compression is effective.

### 6.3 Experiment 3: Single-LoRA vs. Multi-Adapter Comparison

**Objective:** Test the working hypothesis that a single LoRA adapter outperforms multiple smaller adapters.

**Setup:**
- Use a dataset of 200K samples.
- Train one single LoRA adapter with r=64.
- Train two LoRA adapters each with r=32, applied sequentially or in parallel depending on architecture.
- Compare validation loss, inference latency, and memory footprint.

**Success Criteria:**
- The single LoRA adapter achieves equal or lower validation loss compared to the multi-adapter setup.
- Inference latency is reduced for the single-LoRA configuration.
- Memory footprint is smaller or comparable for the single-LoRA approach.

**Outcome:** Validate the single-LoRA advantage hypothesis and confirm it as a preferred deployment strategy.

## 7. Summary

This design document proposes a parametric memory vision based on Growing-LoRA adapters with rank scaled to data volume. The approach uses a small base model, larger LoRA ranks, Fisher-Merge compression via mergekit, and train/valid loss divergence as the overfitting metric. Three experiments are defined to empirically test rank scaling behavior, data-volume-dependent rank selection, and the single-LoRA advantage hypothesis.
