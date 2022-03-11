from pathlib import Path

import hydra
import numpy as np
import torch
from hydra.utils import to_absolute_path
from nnsvs.base import PredictionType
from nnsvs.pitch import nonzero_segments
from nnsvs.train_util import save_checkpoint, setup
from nnsvs.util import make_non_pad_mask
from omegaconf import DictConfig
from torch import nn
from tqdm import tqdm


def note_segments(lf0_score_denorm):
    """Compute note segments (start and end indices) from log-F0

    Note that unvoiced frames must be set to 0 in advance.

    Args:
        lf0_score_denorm (Tensor): (B, T)

    Returns:
        list: list of note (start, end) indices
    """
    segments = []
    for s, e in nonzero_segments(lf0_score_denorm):
        out = torch.sign(torch.abs(torch.diff(lf0_score_denorm[s : e + 1])))
        transitions = torch.where(out > 0)[0]
        note_start, note_end = s, -1
        for pos in transitions:
            note_end = int(s + pos)
            segments.append((note_start, note_end))
            note_start = note_end

    return segments


def pitch_regularization_weight(segments, N, decay_size=25, max_w=0.5):
    """Compute pitch regularization weight given note segments

    Args:
        segments (list): list of note (start, end) indices
        N (int): number of frames
        decay_size (int): size of the decay window
        max_w (float): maximum weight

    Returns:
        Tensor: weights of shape (N,)
    """
    w = torch.zeros(N)

    for s, e in segments:
        L = e - s
        w[s:e] = max_w
        if L > decay_size * 2:
            w[s : s + decay_size] *= torch.arange(decay_size) / decay_size
            w[e - decay_size : e] *= torch.arange(decay_size - 1, -1, -1) / decay_size

    return w


def batch_pitch_regularization_weight(lf0_score_denorm):
    """Batch version of computing pitch regularization weight

    Args:
        lf0_score_denorm (Tensor): (B, T)

    Returns:
        Tensor: weights of shape (B, N, 1)
    """
    B, T = lf0_score_denorm.shape
    w = torch.zeros_like(lf0_score_denorm)
    for idx in range(len(lf0_score_denorm)):
        segments = note_segments(lf0_score_denorm[idx])
        w[idx, :] = pitch_regularization_weight(segments, T).to(w.device)

    return w.unsqueeze(-1)


def train_step(
    model,
    optimizer,
    train,
    in_feats,
    out_feats,
    lengths,
    pitch_reg_w,
):
    optimizer.zero_grad()

    criterion = nn.MSELoss(reduction="none")

    # Apply preprocess if required (e.g., FIR filter for shallow AR)
    # defaults to no-op
    out_feats = model.preprocess_target(out_feats)

    # Run forward
    assert model.prediction_type != PredictionType.PROBABILISTIC
    pred_out_feats, lf0_residual = model(in_feats, lengths)

    # Compute loss
    mask = make_non_pad_mask(lengths).unsqueeze(-1).to(in_feats.device)

    loss = criterion(
        pred_out_feats.masked_select(mask), out_feats.masked_select(mask)
    ).mean()

    # Pitch reguralization
    # NOTE: l1 loss seems to be better than mse loss in my experiments
    # we could use l2 loss as suggested sinsy's paper
    loss += (pitch_reg_w * lf0_residual.abs()).masked_select(mask).mean()

    if train:
        loss.backward()
        optimizer.step()

    return loss


def train_loop(
    config,
    logger,
    device,
    model,
    optimizer,
    lr_scheduler,
    data_loaders,
    writer,
    in_scaler,
    out_scaler,
):
    out_dir = Path(to_absolute_path(config.train.out_dir))
    best_loss = torch.finfo(torch.float32).max

    in_lf0_idx = config.data.in_lf0_idx
    in_rest_idx = config.data.in_rest_idx
    if in_lf0_idx is None or in_rest_idx is None:
        raise ValueError("in_lf0_idx and in_rest_idx must be specified")

    for epoch in tqdm(range(1, config.train.nepochs + 1)):
        for phase in data_loaders.keys():
            train = phase.startswith("train")
            model.train() if train else model.eval()
            running_loss = 0
            for in_feats, out_feats, lengths in data_loaders[phase]:
                # NOTE: This is needed for pytorch's PackedSequence
                lengths, indices = torch.sort(lengths, dim=0, descending=True)
                in_feats, out_feats = (
                    in_feats[indices].to(device),
                    out_feats[indices].to(device),
                )
                # Compute denormalized log-F0 in the musical scores
                lf0_score_denorm = (
                    in_feats[:, :, in_lf0_idx]
                    * float(
                        in_scaler.data_min_[in_lf0_idx]
                        - in_scaler.data_min_[in_lf0_idx]
                    )
                    + in_scaler.data_min_[in_lf0_idx]
                )
                # Fill zeros for rest and padded frames
                lf0_score_denorm *= (in_feats[:, :, in_rest_idx] <= 0).float()
                for idx, length in enumerate(lengths):
                    lf0_score_denorm[idx, length:] = 0
                # Compute pitch regularization weight
                pitch_reg_w = batch_pitch_regularization_weight(lf0_score_denorm)

                loss = train_step(
                    model, optimizer, train, in_feats, out_feats, lengths, pitch_reg_w
                )
                running_loss += loss.item()
            ave_loss = running_loss / len(data_loaders[phase])
            writer.add_scalar(f"Loss/{phase}", ave_loss, epoch)

            ave_loss = running_loss / len(data_loaders[phase])
            logger.info("[%s] [Epoch %s]: loss %s", phase, epoch, ave_loss)
            if not train and ave_loss < best_loss:
                best_loss = ave_loss
                save_checkpoint(
                    logger, out_dir, model, optimizer, lr_scheduler, epoch, is_best=True
                )

        lr_scheduler.step()
        if epoch % config.train.checkpoint_epoch_interval == 0:
            save_checkpoint(
                logger, out_dir, model, optimizer, lr_scheduler, epoch, is_best=False
            )

    save_checkpoint(
        logger, out_dir, model, optimizer, lr_scheduler, config.train.nepochs
    )
    logger.info("The best loss was %s", best_loss)


def _check_resf0_config(logger, model, config, in_scaler, out_scaler):
    logger.info("Checking model configs for residual F0 prediction")
    if in_scaler is None or out_scaler is None:
        raise ValueError("in_scaler and out_scaler must be specified")

    in_lf0_idx = config.data.in_lf0_idx
    in_rest_idx = config.data.in_rest_idx
    if in_lf0_idx is None or in_rest_idx is None:
        raise ValueError("in_lf0_idx and in_rest_idx must be specified")

    logger.info("in_lf0_idx: %s", in_lf0_idx)
    logger.info("in_rest_idx: %s", in_rest_idx)

    ok = True
    if hasattr(model, "in_lf0_idx"):
        if model.in_lf0_idx != in_lf0_idx:
            logger.warn(
                "in_lf0_idx in model and data config must be same",
                model.in_lf0_idx,
                in_lf0_idx,
            )
            ok = False

    if hasattr(model, "in_lf0_min") and hasattr(model, "in_lf0_max"):
        logger.info("in_lf0_min: %s", model.in_lf0_min)
        logger.info("in_lf0_max: %s", model.in_lf0_max)
        if not np.allclose(model.in_lf0_min, in_scaler.data_min_[model.in_lf0_idx]):
            logger.warn(
                f"in_lf0_min is set to {model.in_lf0_min}, "
                "but should be {in_scaler.data_min_[model.in_lf0_idx]}"
            )
            ok = False
        if not np.allclose(model.in_lf0_max, in_scaler.data_max_[model.in_lf0_idx]):
            logger.warn(
                f"in_lf0_max is set to {model.in_lf0_max}, "
                "but should be {in_scaler.data_max_[model.in_lf0_idx]}"
            )
            ok = False

    if hasattr(model, "out_lf0_mean") and hasattr(model, "out_lf0_scale"):
        logger.info("model.out_lf0_idx: %s", model.out_lf0_idx)
        logger.info("model.out_lf0_mean: %s", model.out_lf0_mean)
        logger.info("model.out_lf0_scale: %s", model.out_lf0_scale)
        if not np.allclose(model.out_lf0_mean, out_scaler.mean_[model.out_lf0_idx]):
            logger.warn(
                f"out_lf0_mean is set to {model.out_lf0_mean}, "
                "but should be {out_scaler.mean_[model.out_lf0_idx]}"
            )
            ok = False
        if not np.allclose(model.out_lf0_scale, out_scaler.scale_[model.out_lf0_idx]):
            logger.warn(
                f"out_lf0_scale is set to {model.out_lf0_scale}, "
                "but should be {out_scaler.scale_[model.out_lf0_idx]}"
            )
            ok = False

    if not ok:
        if (
            model.in_lf0_idx == in_lf0_idx
            and hasattr(model, "in_lf0_min")
            and hasattr(model, "out_lf0_mean")
        ):
            logger.info(
                f"""
If you are 100% sure that you set model.in_lf0_idx and model.out_lf0_idx correctly,
Please consider the following parameters in your model config:

    in_lf0_idx: {model.in_lf0_idx}
    out_lf0_idx: {model.out_lf0_idx}
    in_lf0_min: {in_scaler.data_min_[model.in_lf0_idx]}
    in_lf0_max: {in_scaler.data_max_[model.in_lf0_idx]}
    out_lf0_mean: {out_scaler.mean_[model.out_lf0_idx]}
    out_lf0_scale: {out_scaler.scale_[model.out_lf0_idx]}
"""
            )
        raise ValueError("The model config has wrong configurations.")


@hydra.main(config_path="conf/train_resf0", config_name="config")
def my_app(config: DictConfig) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    (
        model,
        optimizer,
        lr_scheduler,
        data_loaders,
        writer,
        logger,
        in_scaler,
        out_scaler,
    ) = setup(config, device)

    _check_resf0_config(logger, model, config, in_scaler, out_scaler)

    train_loop(
        config,
        logger,
        device,
        model,
        optimizer,
        lr_scheduler,
        data_loaders,
        writer,
        in_scaler,
        out_scaler,
    )


def entry():
    my_app()


if __name__ == "__main__":
    my_app()
