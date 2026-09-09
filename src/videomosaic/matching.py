"""Pure temporal matching and bounded per-cell color correction."""

import cv2
import numpy as np
from scipy.spatial.distance import cdist


def match_sources(source_means, target_means, seed=937):
    frames, rows, columns, _ = target_means.shape
    sources = source_means.shape[1]
    cells = rows * columns
    if source_means.shape != (frames, sources, 3) or cells < sources:
        raise ValueError("Source/target shapes or source coverage are invalid")
    sample = np.linspace(0, frames - 1, min(16, frames)).astype(int)
    source_lab = cv2.cvtColor(source_means.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    target_lab = cv2.cvtColor(
        target_means.reshape(frames, cells, 3).astype(np.float32) / 255, cv2.COLOR_RGB2LAB
    )
    source_features = source_lab[sample].transpose(1, 0, 2).reshape(sources, -1)
    target_features = target_lab[sample].transpose(1, 0, 2).reshape(cells, -1)
    costs = (cdist(target_features, source_features, "sqeuclidean") / len(sample)).astype(
        np.float32
    )
    alternative = min(5, sources - 1)
    difficulty = np.partition(costs, alternative, axis=1)[:, alternative]
    rng = np.random.default_rng(seed)
    order = np.argsort(-(difficulty + rng.uniform(0, 0.01, cells)))
    assignment = np.full(cells, -1, np.int32)
    counts = np.zeros(sources, np.int32)
    average = cells / sources
    for cell in order:
        row, col = divmod(int(cell), columns)
        score = costs[cell] + 20 * (counts / average) ** 2
        score[counts >= np.ceil(average * 2.3)] += 100000
        neighbors = {
            assignment[r * columns + c]
            for r in range(max(0, row - 1), min(rows, row + 2))
            for c in range(max(0, col - 1), min(columns, col + 2))
            if assignment[r * columns + c] >= 0
        }
        score[list(neighbors)] += 100000
        idx = int(score.argmin())
        assignment[cell] = idx
        counts[idx] += 1
    for idx in np.where(counts == 0)[0]:
        delta = costs[:, idx] - costs[np.arange(cells), assignment]
        delta[counts[assignment] <= 1] = np.inf
        cell = int(delta.argmin())
        counts[assignment[cell]] -= 1
        assignment[cell] = idx
        counts[idx] += 1
    return assignment.reshape(rows, columns)


def correct(tile, source_mean, target_mean, strength=0.96, detail=0.68):
    center = (1 - strength) * source_mean + strength * target_mean
    weights = np.array([0.2126, 0.7152, 0.0722])
    luminance, original = float(center @ weights), float(source_mean @ weights)
    gamut = min(
        1.0,
        (luminance + 18) / (original * 0.6 + 18),
        (273 - luminance) / max(18, 255 - original * 0.6),
    )
    gain = detail * max(0.12, gamut)
    return np.clip(center + (tile.astype(np.float32) - source_mean) * gain, 0, 255).astype(np.uint8)
