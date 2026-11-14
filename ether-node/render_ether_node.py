#!/usr/bin/env python3
"""
Ether Node - a seamless 6 second procedural loop.

A luminous, breathing spherical particle node with fine energy filaments and
faint neural links. Rendered entirely with numpy, composited over warm white,
and encoded with ffmpeg (imageio-ffmpeg).

Usage:
    python render_ether_node.py preview --scale 0.5 --frames 0,90,180
    python render_ether_node.py video --out ether-node-loop.mp4 --fps 60
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, zoom


TAU = 2.0 * math.pi
DURATION = 6.0
BASE_W, BASE_H = 1920, 1080
BASE_R = 248.0
BASE_CX, BASE_CY = 960.0, 512.0


# ---------------------------------------------------------------------------
# small helpers


def unit(v: np.ndarray, axis: int = -1) -> np.ndarray:
    n = np.linalg.norm(v, axis=axis, keepdims=True)
    return v / np.maximum(n, 1e-9)


def random_unit(rng: np.random.Generator, n: int) -> np.ndarray:
    v = rng.normal(size=(n, 3))
    return unit(v)


def interpolate_palette(stops, x):
    """stops: list of (position, (r,g,b)); x: array in [0,1]."""
    pos = np.array([s[0] for s in stops], dtype=np.float32)
    cols = np.array([s[1] for s in stops], dtype=np.float32)
    x = np.clip(x, 0.0, 1.0)
    out = np.empty(x.shape + (3,), dtype=np.float32)
    for c in range(3):
        out[..., c] = np.interp(x, pos, cols[:, c])
    return out


def gaussian_stamp(radius: int, sigma: float):
    ax = np.arange(-radius, radius + 1, dtype=np.float32)
    xx, yy = np.meshgrid(ax, ax)
    k = np.exp(-(xx * xx + yy * yy) / (2.0 * sigma * sigma))
    k /= k.sum()
    return [(int(dx), int(dy), float(k[dy + radius, dx + radius]))
            for dy in range(-radius, radius + 1)
            for dx in range(-radius, radius + 1)]


K3 = gaussian_stamp(1, 0.62)
K5 = gaussian_stamp(2, 1.05)
K7 = gaussian_stamp(3, 1.75)
K11 = gaussian_stamp(5, 3.10)
K15 = gaussian_stamp(7, 4.20)


def glow_stamp(radius: int, sigma: float, floor: float = 0.0):
    """Peak-normalised stamp: a bright centre with a soft coloured skirt.

    Unlike a normalised gaussian this keeps a single mote visible against a
    white field, while still contributing a little light to its neighbours.
    """
    ax = np.arange(-radius, radius + 1, dtype=np.float32)
    xx, yy = np.meshgrid(ax, ax)
    k = np.exp(-(xx * xx + yy * yy) / (2.0 * sigma * sigma))
    k = np.maximum(k, floor)
    return [(int(dx), int(dy), float(k[dy + radius, dx + radius]))
            for dy in range(-radius, radius + 1)
            for dx in range(-radius, radius + 1)]


K3B = glow_stamp(1, 0.66, 0.045)
K5B = glow_stamp(2, 1.00, 0.025)
K7B = glow_stamp(3, 1.45, 0.012)


def lerp(a, b, t):
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# main renderer


class EtherNode:
    def __init__(self, out_w: int = BASE_W, out_h: int = BASE_H, seed: int = 73):
        self.W = int(out_w)
        self.H = int(out_h)
        self.s = self.W / BASE_W
        self.cx = BASE_CX * self.s
        self.cy = BASE_CY * self.s
        self.R = BASE_R * self.s
        self.rng = np.random.default_rng(seed)

        # premultiplied accumulation buffers.  The main pass is the volumetric
        # body; the top pass is composited afterwards so isolated bright motes
        # are not washed out by the dense red core.
        self.E = np.zeros((self.H, self.W, 3), dtype=np.float32)
        self.A = np.zeros((self.H, self.W), dtype=np.float32)
        self.topE = np.zeros((self.H, self.W, 3), dtype=np.float32)
        self.topA = np.zeros((self.H, self.W), dtype=np.float32)

        self._make_particles()
        self._make_strands()
        self._make_core_knot()
        self._make_streams()
        self._make_filaments()
        self._make_connections()
        self._make_sparkles()
        self._make_background()

        # A fixed subset of motes feeds the broad volumetric tint. Everything
        # random here is precomputed, never sampled per frame, so the loop is
        # bit-for-bit periodic.
        self.haze_sel = np.arange(0, self.dust["x0"].size, 4)
        self.haze_w = 0.014 + 0.016 * self.rng.random(self.haze_sel.size)
        self.haze_col = (0.72 * self.dust["color"][self.haze_sel] +
                         0.28 * np.array([1.0, 0.30, 0.035], dtype=np.float32))

        # low resolution buffers for volumetric haze
        self.hz = max(3, int(round(4 * self.s)))
        self.hw = int(math.ceil(self.W / self.hz))
        self.hh = int(math.ceil(self.H / self.hz))
        self.haze_E = np.zeros((self.hh, self.hw, 3), dtype=np.float32)
        self.haze_A = np.zeros((self.hh, self.hw), dtype=np.float32)
        self.haze_yy, self.haze_xx = np.mgrid[0:self.hh, 0:self.hw]

    # ------------------------------------------------------------------
    # setup

    def _make_particles(self):
        rng = self.rng
        parts = []

        # --- dense core dust: deep crimson, packed toward the middle
        n = 5000
        u = rng.random(n)
        r = 0.040 + 0.64 * (u ** 1.42)
        parts.append(self._particle_block(
            n=n, radius=r, kind="core",
            size=(0.55, 1.40), alpha=(0.68, 1.00),
            swirl_mix=(0.54, 0.16, 0.30),  # +1, 0, -1
            wobble=(0.012, 0.030),
        ))

        # --- a hot, ember-like ganglion at the very centre
        n = 600
        u = rng.random(n)
        r = 0.015 + 0.135 * (u ** 0.85)
        parts.append(self._particle_block(
            n=n, radius=r, kind="hot",
            size=(0.65, 1.60), alpha=(0.72, 1.00),
            swirl_mix=(0.70, 0.10, 0.20),
            wobble=(0.010, 0.028),
        ))

        # --- bright embers threaded through the inner body
        n = 800
        u = rng.random(n)
        r = 0.03 + 0.34 * (u ** 0.95)
        parts.append(self._particle_block(
            n=n, radius=r, kind="ember",
            size=(0.65, 1.75), alpha=(0.78, 1.00),
            swirl_mix=(0.66, 0.12, 0.22),
            wobble=(0.012, 0.034),
        ))

        # --- the main shell / mid-body
        n = 9000
        u = rng.random(n)
        r = 0.29 + 0.72 * (u ** 0.78)
        r = np.clip(r, 0.0, 1.02)
        parts.append(self._particle_block(
            n=n, radius=r, kind="shell",
            size=(0.55, 1.60), alpha=(0.58, 1.00),
            swirl_mix=(0.58, 0.22, 0.20),
            wobble=(0.015, 0.040),
        ))

        # --- a clearly articulated, bright rim: the sphere must read as a body
        n = 2600
        u = rng.random(n)
        r = 0.925 + 0.105 * (u ** 0.72)
        parts.append(self._particle_block(
            n=n, radius=r, kind="rim",
            size=(0.55, 1.40), alpha=(0.55, 0.95),
            swirl_mix=(0.72, 0.08, 0.20),
            wobble=(0.008, 0.020),
        ))

        # --- sparse halo and escaping wisps
        n = 1500
        u = rng.random(n)
        r = 1.005 + 0.36 * (u ** 1.60)
        parts.append(self._particle_block(
            n=n, radius=r, kind="halo",
            size=(0.55, 1.65), alpha=(0.42, 0.90),
            swirl_mix=(0.50, 0.30, 0.20),
            wobble=(0.020, 0.075),
        ))

        # --- coherent arcs of bright particles on the surface. Grouping them
        # into streams is what makes the surface read as flowing currents
        # rather than evenly sprinkled dust.
        n_streams = 15
        orbit_x, orbit_y, orbit_z = [], [], []
        orbit_rad, orbit_phase, orbit_speed = [], [], []
        orbit_size, orbit_alpha, orbit_color = [], [], []
        orbit_wob, orbit_ph2 = [], []
        for k in range(n_streams):
            count = int(rng.integers(34, 64))
            axis = random_unit(rng, 1)[0]
            p = unit(np.cross(axis, random_unit(rng, 1)[0]))
            q = np.cross(axis, p)
            span = rng.uniform(0.55, 1.45)
            centre = rng.random() * TAU
            phases = centre + np.linspace(-span / 2, span / 2, count)
            phases += rng.normal(0.0, 0.018, count)
            orbit_x.append(np.repeat(p[None, :], count, axis=0))
            orbit_y.append(np.repeat(q[None, :], count, axis=0))
            orbit_z.append(np.repeat(axis[None, :], count, axis=0))
            orbit_rad.append(0.985 + 0.055 * rng.random(count))
            orbit_phase.append(phases)
            orbit_speed.append(np.full(count, rng.choice([1, 1, 1, 2])))
            orbit_size.append(1.2 + 1.9 * rng.random(count))
            orbit_alpha.append(0.64 + 0.34 * rng.random(count))
            col = interpolate_palette(
                [(0.0, (1.0, 0.42, 0.010)), (0.50, (1.0, 0.58, 0.020)),
                 (1.0, (1.0, 0.79, 0.075))], np.array([rng.random()]))
            orbit_color.append(np.tile(col, (count, 1)))
            orbit_wob.append(0.010 + 0.022 * rng.random(count))
            orbit_ph2.append(rng.random(count) * TAU)

        def cat(lst):
            return np.concatenate(lst, axis=0)

        parts.append(dict(
            kind="orbit", x0=cat(orbit_x), y0=cat(orbit_y), z0=cat(orbit_z),
            radius=cat(orbit_rad), phase=cat(orbit_phase), speed=cat(orbit_speed),
            size=cat(orbit_size), alpha=cat(orbit_alpha), color=cat(orbit_color),
            wob=cat(orbit_wob), ph2=cat(orbit_ph2),
        ))

        # --- tiny warm glints inside / just outside the body
        n = 170
        d = random_unit(rng, n)
        r = 0.35 + 0.95 * (rng.random(n) ** 0.85)
        parts.append(dict(
            kind="glint", dir=d, radius=r,
            phase=rng.random(n) * TAU,
            freq=rng.choice(np.array([1, 2, 3], dtype=np.int32), size=n),
            size=0.8 + 1.9 * rng.random(n),
            color=interpolate_palette(
                [(0.0, (1.0, 0.44, 0.09)), (0.55, (1.0, 0.68, 0.18)),
                 (1.0, (1.0, 0.88, 0.48))], rng.random(n)),
            alpha=0.35 + 0.45 * rng.random(n),
        ))

        # ---- concatenate the independent dust blocks
        blocks = [p for p in parts if "x0" in p and p.get("kind") not in ("orbit", "glint")]
        self.dust = {key: np.concatenate([p[key] for p in blocks], axis=0)
                     for key in ("x0", "y0", "z0", "r0", "size", "alpha",
                                 "color", "swirl", "ph1", "ph2", "ph3", "ph4",
                                 "wob1", "wob2")}
        self.orbit = [p for p in parts if p.get("kind") == "orbit"][0]
        self.glint = [p for p in parts if p.get("kind") == "glint"][0]

    def _particle_block(self, n, radius, kind, size, alpha, swirl_mix, wobble):
        rng = self.rng
        d = random_unit(rng, n)

        if kind == "hot":
            stops = [(0.0, (0.96, 0.12, 0.010)),
                     (0.45, (1.00, 0.31, 0.008)),
                     (0.78, (1.00, 0.50, 0.018)),
                     (1.0, (1.00, 0.72, 0.080))]
        elif kind == "ember":
            stops = [(0.0, (0.82, 0.035, 0.008)),
                     (0.38, (1.00, 0.16, 0.006)),
                     (0.72, (1.00, 0.34, 0.008)),
                     (1.0, (1.00, 0.56, 0.030))]
        elif kind == "core":
            stops = [(0.0, (0.78, 0.020, 0.032)),
                     (0.34, (0.90, 0.040, 0.022)),
                     (0.68, (0.99, 0.100, 0.012)),
                     (1.0, (1.00, 0.240, 0.010))]
        elif kind == "shell":
            stops = [(0.0, (0.56, 0.015, 0.004)),
                     (0.20, (0.84, 0.075, 0.003)),
                     (0.45, (1.00, 0.205, 0.002)),
                     (0.70, (1.00, 0.370, 0.004)),
                     (1.0, (1.00, 0.560, 0.020))]
        elif kind == "rim":
            stops = [(0.0, (0.94, 0.230, 0.006)),
                     (0.40, (1.00, 0.390, 0.006)),
                     (0.74, (1.00, 0.550, 0.014)),
                     (1.0, (1.00, 0.720, 0.055))]
        else:
            stops = [(0.0, (0.98, 0.400, 0.004)),
                     (0.34, (1.00, 0.550, 0.008)),
                     (0.68, (1.00, 0.680, 0.018)),
                     (1.0, (1.00, 0.830, 0.095))]

        rnorm = np.clip(radius / 1.6, 0.0, 1.0)
        base_col = interpolate_palette(stops, rnorm)
        # individual chromatic variation, and a little warm light from top-left
        base_col *= (0.90 + 0.18 * rng.random(n))[:, None]
        base_col[:, 0] = np.clip(base_col[:, 0], 0.0, 1.0)
        base_col[:, 1] = np.clip(base_col[:, 1] * (0.92 + 0.16 * rng.random(n)), 0.0, 1.0)
        base_col[:, 2] = np.clip(base_col[:, 2] * (0.85 + 0.25 * rng.random(n)), 0.0, 1.0)

        mix = np.array(swirl_mix, dtype=np.float64)
        mix /= mix.sum()
        swirl = rng.choice(np.array([1, 0, -1], dtype=np.int32), size=n, p=mix)

        return dict(
            kind=kind,
            x0=d[:, 0].copy(), y0=d[:, 1].copy(), z0=d[:, 2].copy(),
            r0=radius.copy(),
            size=rng.uniform(size[0], size[1], n),
            alpha=rng.uniform(alpha[0], alpha[1], n),
            color=base_col,
            swirl=swirl,
            ph1=rng.random(n) * TAU,
            ph2=rng.random(n) * TAU,
            ph3=rng.random(n) * TAU,
            ph4=rng.random(n) * TAU,
            wob1=rng.uniform(wobble[0], wobble[1], n),
            wob2=rng.uniform(wobble[0] * 0.5, wobble[1] * 1.6, n),
        )

    def _make_strands(self):
        """Hundreds of short curved fibres: the structural 'silk' of the node."""
        rng = self.rng
        n = 150
        samples = 94

        d = random_unit(rng, n)
        rr = 0.24 + 0.78 * (rng.random(n) ** 0.72)
        rr = np.clip(rr, 0.12, 1.03)
        base = d * rr[:, None]
        axis = random_unit(rng, n)
        span = 2.15 + 3.30 * (rng.random(n) ** 0.85)
        phase = rng.random(n) * TAU
        swirl = rng.choice(np.array([1, 1, 0, -1], dtype=np.int32), n)
        col = interpolate_palette(
            [(0.0, (0.95, 0.045, 0.010)), (0.30, (1.0, 0.13, 0.006)),
             (0.58, (1.0, 0.30, 0.008)), (0.80, (1.0, 0.47, 0.018)),
             (1.0, (1.0, 0.68, 0.060))],
            np.clip(rr / 1.05, 0, 1))
        col *= (0.90 + 0.18 * rng.random(n))[:, None]
        self.strands = dict(
            base=base.astype(np.float32), axis=axis.astype(np.float32),
            r=rr.astype(np.float32), span=span.astype(np.float32),
            phase=phase.astype(np.float32), swirl=swirl,
            color=np.clip(col, 0, 1).astype(np.float32),
            alpha=(0.32 + 0.24 * rng.random(n)).astype(np.float32),
            width=(0.14 + 0.16 * rng.random(n)).astype(np.float32),
            s=np.linspace(-0.5, 0.5, samples, dtype=np.float32),
        )

    def _make_core_knot(self):
        """A tight tangle of gold filaments at the heart of the node."""
        rng = self.rng
        n = 42
        samples = 60
        d = random_unit(rng, n)
        rr = 0.015 + 0.245 * (rng.random(n) ** 0.72)
        base = d * rr[:, None]
        axis = random_unit(rng, n)
        span = 1.7 + 4.0 * rng.random(n)
        phase = rng.random(n) * TAU
        col = interpolate_palette(
            [(0.0, (1.0, 0.240, 0.008)), (0.45, (1.0, 0.400, 0.012)),
             (0.78, (1.0, 0.580, 0.025)), (1.0, (1.0, 0.760, 0.080))],
            rng.random(n))
        self.core_knot = dict(
            base=base.astype(np.float32), axis=axis.astype(np.float32),
            r=rr.astype(np.float32), span=span.astype(np.float32),
            phase=phase.astype(np.float32), color=np.clip(col, 0, 1).astype(np.float32),
            alpha=(0.30 + 0.22 * rng.random(n)).astype(np.float32),
            s=np.linspace(-0.5, 0.5, samples, dtype=np.float32),
        )

    def _make_streams(self):
        """Long, luminous currents winding through the volume.

        These are deliberately coherent arcs: they give the eye a path through
        the cloud instead of a uniform field of dots.
        """
        rng = self.rng
        xs, ys, zs = [], [], []
        radii, sizes, alphas, colors = [], [], [], []
        phases, swirls, beats = [], [], []
        for k in range(12):
            count = int(rng.integers(280, 430))
            t = np.linspace(0.0, 1.0, count)
            t = np.clip(t + rng.normal(0.0, 0.006, count), 0.0, 1.0)
            turns = rng.choice(np.array([-1, 1]))
            lat = -1.16 + 2.32 * t + rng.normal(0.0, 0.026, count)
            lon = rng.random() * TAU + turns * TAU * t + rng.normal(0.0, 0.045, count)
            rr = 0.34 + 0.58 * (np.sin(np.pi * t) ** 0.55)
            rr += rng.normal(0.0, 0.028, count)
            rr = np.clip(rr, 0.18, 0.98)
            x = rr * np.cos(lat) * np.cos(lon)
            y = rr * np.sin(lat)
            z = rr * np.cos(lat) * np.sin(lon)
            xs.append(x * self.R)
            ys.append(y * self.R)
            zs.append(z * self.R)
            radii.append(rr)
            sizes.append(0.95 + 1.75 * rng.random(count))
            alphas.append(0.34 + 0.42 * rng.random(count))
            col = interpolate_palette(
                [(0.0, (1.0, 0.17, 0.006)), (0.45, (1.0, 0.36, 0.010)),
                 (0.75, (1.0, 0.54, 0.030)), (1.0, (1.0, 0.72, 0.080))],
                np.clip((rr - 0.30) / 0.68, 0, 1))
            colors.append(np.clip(col * (0.92 + 0.16 * rng.random(count))[:, None], 0, 1))
            phases.append(rng.random(count) * TAU)
            swirls.append(np.full(count, turns, dtype=np.int32))
            beats.append(rng.random(count) * TAU)

        def cat(lst):
            return np.concatenate(lst, axis=0)

        self.streams = dict(
            x0=cat(xs), y0=cat(ys), z0=cat(zs), r=cat(radii),
            size=cat(sizes), alpha=cat(alphas), color=cat(colors),
            phase=cat(phases), swirl=cat(swirls), beat=cat(beats),
        )

    def _make_filaments(self):
        rng = self.rng
        n = 30
        u = np.linspace(0.0, TAU, 190, endpoint=False, dtype=np.float32)
        uu = u[None, :]

        a = random_unit(rng, n)
        b = unit(np.cross(a, random_unit(rng, n)))
        c = np.cross(a, b)
        phase1 = rng.random((n, 1)) * TAU
        phase2 = rng.random((n, 1)) * TAU
        phase3 = rng.random((n, 1)) * TAU
        base_r = 0.83 + 0.12 * rng.random((n, 1))
        amp = 0.06 + 0.09 * rng.random((n, 1))
        normal_amp = 0.035 + 0.05 * rng.random((n, 1))
        swirl = rng.choice(np.array([1, 1, 1, 0], dtype=np.int32), n)
        speed = rng.choice(np.array([1, 1, 2], dtype=np.int32), n)

        # base curve in 3D, shape (n, L, 3)
        base = (a[:, None, :] * np.cos(uu)[..., None] +
                b[:, None, :] * np.sin(uu)[..., None])
        base = base * (base_r + amp * np.sin(2 * uu + phase1))[..., None]
        base = base + c[:, None, :] * (normal_amp * np.sin(3 * uu + phase2))[..., None]

        colmix = rng.random(n)
        colors = interpolate_palette(
            [(0.0, (0.96, 0.16, 0.035)), (0.48, (1.0, 0.41, 0.06)),
             (1.0, (1.0, 0.73, 0.22))], colmix)
        self.filaments = dict(
            base=base.astype(np.float32), u=u, swirl=swirl,
            phase=phase3[:, 0], speed=speed, color=colors,
            alpha=0.16 + 0.14 * rng.random(n),
            beat=rng.random(n) * TAU,
        )

    def _make_connections(self):
        rng = self.rng
        n = 11

        # endpoints gather in a few loose clusters, so the links read as a
        # network around the node rather than a regular solar corona
        cluster = rng.integers(0, 4, n)
        centres = np.array([0.25, 1.75, 3.45, 5.05])
        az = centres[cluster] + rng.normal(0.0, 0.24, n)
        el = np.clip(rng.normal(0.10, 0.58, n), -0.85, 0.88)
        dist = 1.24 + 0.50 * (rng.random(n) ** 0.72)

        # Keep the terminals close to the screen plane.  Otherwise half of the
        # links point at / away from the camera and their nodes disappear
        # behind the node body, which destroys the surrounding network.
        rxy = np.cos(el)
        d_end = np.stack(
            [rxy * np.cos(az), np.sin(el), 0.26 * rxy * np.sin(az)],
            axis=1)
        d_end = unit(d_end)
        # start is a point on the surface, offset around the same direction
        d_start = unit(d_end + rng.normal(0.0, 0.24, (n, 3)))
        d_start[:, 1] = np.clip(d_start[:, 1], -0.85, 0.90)
        d_start = unit(d_start)

        # two control directions for an organic, slightly bowed tendril
        side = unit(np.cross(d_end, np.array([0.0, 1.0, 0.0])[None, :]) +
                    rng.normal(0.0, 0.35, (n, 3)))
        side = unit(side)
        lift = unit(d_end + rng.normal(0.0, 0.18, (n, 3)))

        self.connections = dict(
            d_start=d_start.astype(np.float32),
            d_end=d_end.astype(np.float32),
            dist=dist.astype(np.float32),
            side=side.astype(np.float32),
            lift=lift.astype(np.float32),
            phase=rng.random(n) * TAU,
            wide=rng.uniform(0.17, 0.42, n),
            speed=rng.choice(np.array([1, 1, 2], dtype=np.int32), n),
            alpha=0.34 + 0.20 * rng.random(n),
            color=interpolate_palette(
                [(0.0, (0.92, 0.14, 0.006)), (0.52, (1.0, 0.38, 0.012)),
                 (1.0, (1.0, 0.64, 0.050))], rng.random(n)),
        )

        # travelling data packets, one or two per link
        packets = []
        for i in range(n):
            for k in range(2 if rng.random() < 0.45 else 1):
                packets.append((
                    i, float(rng.random()), int(rng.choice([1, 1, 2])),
                    float(rng.uniform(0.45, 0.95)),
                ))
        self.packets = packets

    def _make_sparkles(self):
        rng = self.rng
        n = 210
        d = random_unit(rng, n)
        r = 0.90 + 0.52 * rng.random(n)
        self.sparkles = dict(
            x0=d[:, 0] * r, y0=d[:, 1] * r, z0=d[:, 2] * r,
            phase=rng.random(n) * TAU,
            freq=rng.choice(np.array([2, 3, 4, 5], dtype=np.int32), size=n),
            size=0.75 + 1.8 * rng.random(n),
            sway=0.03 + 0.09 * rng.random(n),
            color=interpolate_palette(
                [(0.0, (1.0, 0.62, 0.17)), (0.7, (1.0, 0.80, 0.36)),
                 (1.0, (1.0, 0.92, 0.62))], rng.random(n)),
        )

        # hot gold seeds in the very middle: a granular heart, not a flat disc
        m = 190
        d = random_unit(rng, m)
        r = 0.012 + 0.30 * (rng.random(m) ** 0.80)
        self.core_sparks = dict(
            x0=d[:, 0] * r, y0=d[:, 1] * r, z0=d[:, 2] * r,
            phase=rng.random(m) * TAU,
            size=1.0 + 2.1 * rng.random(m),
            alpha=0.72 + 0.28 * rng.random(m),
            color=interpolate_palette(
                [(0.0, (1.0, 0.25, 0.010)), (0.55, (1.0, 0.46, 0.015)),
                 (1.0, (1.0, 0.78, 0.12))], rng.random(m)),
        )

    def _make_background(self):
        y, x = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        xb = (x + 0.5) / self.s
        yb = (y + 0.5) / self.s
        warm = np.exp(-(((xb - 960.0) / 780.0) ** 2 +
                        ((yb - 510.0) / 680.0) ** 2) * 1.8)
        bg = np.empty((self.H, self.W, 3), dtype=np.float32)
        bg[..., 0] = 1.0
        bg[..., 1] = 1.0 - 0.0055 * warm
        bg[..., 2] = 1.0 - 0.014 * warm

        # very soft warm ambient bloom around the node
        rr = np.sqrt(((xb - 960.0) / 560.0) ** 2 + ((yb - 500.0) / 520.0) ** 2)
        aura = 0.085 * np.exp(-(rr ** 2) * 2.5)
        aura_col = np.array([1.0, 0.64, 0.30], dtype=np.float32)
        bg = bg * (1.0 - aura[..., None]) + aura_col * aura[..., None]

        # diffuse shadow far below the floating node
        sx = 225.0
        sy = 34.0
        sh = np.exp(-(((xb - 960.0) / sx) ** 2 +
                      ((yb - (BASE_CY + BASE_R + 35.0)) / sy) ** 2) * 1.7)
        sh = gaussian_filter(sh, 5.0 * self.s)
        sh_col = np.array([0.60, 0.47, 0.38], dtype=np.float32)
        sh_a = 0.070 * sh
        bg = bg * (1.0 - sh_a[..., None]) + sh_col * sh_a[..., None]

        self.bg = bg
        self.yg, self.xg = y, x

    # ------------------------------------------------------------------
    # animation helpers

    def _breathe(self, t):
        th = TAU * t / DURATION
        b = 1.0 + 0.043 * math.sin(th) + 0.011 * math.sin(2.0 * th + 0.75)
        return th, b

    def _rotate(self, x, y, z, angle, tilt, yaw=0.0):
        ca, sa = np.cos(angle), np.sin(angle)
        x1 = x * ca + z * sa
        z1 = -x * sa + z * ca
        cb, sb = np.cos(tilt), np.sin(tilt)
        y1 = y * cb - z1 * sb
        z2 = y * sb + z1 * cb
        if yaw is not None:
            cc, sc = np.cos(yaw), np.sin(yaw)
            x2 = x1 * cc - y1 * sc
            y2 = x1 * sc + y1 * cc
            return x2, y2, z2
        return x1, y1, z2

    def _project(self, x, y, z):
        q = z / max(self.R, 1e-6)
        persp = 6.8 / (6.8 - q)
        return self.cx + x * persp, self.cy + y * persp, persp

    # ------------------------------------------------------------------
    # splatting

    def _deposit(self, xs, ys, weights, colors, kernel, scale=1.0, top=False):
        if xs.size == 0:
            return
        colors = np.asarray(colors, dtype=np.float32)
        if colors.ndim == 1:
            colors = colors[None, :]
        if colors.shape[0] == 1 and weights.size > 1:
            colors = np.broadcast_to(colors, (weights.size, 3))
        xi = np.rint(xs * scale).astype(np.int64)
        yi = np.rint(ys * scale).astype(np.int64)
        valid = (xi >= 0) & (xi < self.W) & (yi >= 0) & (yi < self.H)
        if not np.any(valid):
            return
        if not np.all(valid):
            xi = xi[valid]
            yi = yi[valid]
            weights = weights[valid]
            colors = colors[valid]
        flat_a = (self.topA if top else self.A).reshape(-1)
        flat_e = (self.topE if top else self.E).reshape(-1, 3)
        for dx, dy, kw in kernel:
            idx = (yi + dy) * self.W + (xi + dx)
            inside = (idx >= 0) & (idx < flat_a.size)
            if not np.all(inside):
                idx = idx[inside]
            ww = weights[inside] * kw
            if ww.size == 0:
                continue
            np.add.at(flat_a, idx, ww)
            np.add.at(flat_e, idx, ww[:, None] * colors[inside])

    def _deposit_low(self, xs, ys, weights, colors, kernel):
        xi = np.rint(xs / self.hz).astype(np.int64)
        yi = np.rint(ys / self.hz).astype(np.int64)
        valid = (xi >= 0) & (xi < self.hw) & (yi >= 0) & (yi < self.hh)
        if not np.any(valid):
            return
        xi, yi = xi[valid], yi[valid]
        weights, colors = weights[valid], colors[valid]
        fa = self.haze_A.reshape(-1)
        fe = self.haze_E.reshape(-1, 3)
        for dx, dy, kw in kernel:
            idx = (yi + dy) * self.hw + (xi + dx)
            inside = (idx >= 0) & (idx < fa.size)
            if not np.all(inside):
                idx = idx[inside]
            ww = weights[inside] * kw
            if ww.size == 0:
                continue
            np.add.at(fa, idx, ww)
            np.add.at(fe, idx, ww[:, None] * colors[inside])

    # ------------------------------------------------------------------
    # frame

    def render(self, frame: int, nframes: int) -> np.ndarray:
        t = (frame % nframes) / nframes * DURATION
        th, breath = self._breathe(t)
        self.E.fill(0.0)
        self.A.fill(0.0)
        self.topE.fill(0.0)
        self.topA.fill(0.0)
        self.haze_E.fill(0.0)
        self.haze_A.fill(0.0)

        vis = 1.0 - 0.055 * math.sin(th)  # slightly brighter when contracted

        self._draw_haze(th, breath)
        self._draw_dust(th, breath, vis)
        self._draw_strands(th, breath)
        self._draw_streams(th, breath)
        self._draw_filaments(th, breath)
        self._draw_connections(th, breath)
        self._draw_core_knot(th, breath)
        self._draw_core_sparks(th)
        self._draw_orbiters(th, breath)
        self._draw_glints(th)
        self._draw_sparkles(th, breath)

        out = self._composite(th, breath)
        return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)

    # ------------------------------------------------------------------

    def _draw_haze(self, th, breath):
        # Broad, faint volume tint at a quarter of the output resolution.
        d = self.dust
        take = self.haze_sel
        x, y, z = d["x0"][take], d["y0"][take], d["z0"][take]
        r0 = d["r0"][take]
        ph = d["ph1"][take]

        r = r0 * self.R * breath * (1.0 + 0.022 * np.sin(th + ph))
        angle = d["swirl"][take] * th + 0.13 * np.sin(th + d["ph3"][take])
        x, y, z = self._rotate(x, y, z, angle, 0.035 * np.sin(th + d["ph4"][take]))
        x *= r
        y *= r
        z *= r
        xs, ys, _ = self._project(x, y, z)

        w = self.haze_w * (0.85 + 0.15 * np.cos(th))
        self._deposit_low(xs, ys, w, self.haze_col, K11)
        self._deposit_low(xs, ys, w * 0.8, self.haze_col * 0.9, K7)

        hE = gaussian_filter(self.haze_E, sigma=(0.8, 0.8, 0.0))
        hA = gaussian_filter(self.haze_A, sigma=0.8)
        # colour is the weighted average before alpha shaping
        hcol = hE / (hA[..., None] + 1e-6)
        # upsample the very low resolution cloud without hard blocks
        hE = zoom(hE, (self.H / self.hh, self.W / self.hw, 1.0), order=1)
        hA = zoom(hA, (self.H / self.hh, self.W / self.hw), order=1)
        hA = 1.0 - np.exp(-hA * 1.25)
        hA = np.clip(hA * 0.32, 0.0, 0.17)
        hcol = np.clip(zoom(hcol, (self.H / self.hh, self.W / self.hw, 1.0), order=1),
                       0.0, 1.0)
        self.haze_rgb = hcol.astype(np.float32)
        self.haze_alpha = hA.astype(np.float32)

    # ------------------------------------------------------------------

    def _draw_dust(self, th, breath, vis):
        d = self.dust
        n = d["x0"].size
        r0 = d["r0"]

        # local radial breathing plus a flowing displacement
        r = r0 * self.R * breath * (1.0 + d["wob1"] * np.sin(th + d["ph1"]) +
                                    d["wob2"] * np.sin(2.0 * th + d["ph2"]))
        angle = d["swirl"] * th + 0.10 * np.sin(th + d["ph3"])
        tilt = 0.030 * np.sin(th + d["ph4"])
        x, y, z = self._rotate(d["x0"], d["y0"], d["z0"], angle, tilt)

        # fixed warm key light from the upper left gives the cloud a volume
        light = np.clip(0.48 - 0.36 * x + 0.44 * y + 0.42 * z, 0.0, 1.0)
        x *= r
        y *= r
        z *= r

        # subtle directional drift, so the body is never a rigid rotation
        x += 0.006 * self.R * np.sin(2.0 * th + d["ph2"]) * (0.3 + r0)
        y += 0.005 * self.R * np.sin(3.0 * th + d["ph1"]) * (0.2 + r0)

        xs, ys, persp = self._project(x, y, z)
        depth = np.clip(z / self.R, -1.35, 1.35)
        near = 0.22 + 0.86 * (depth + 1.35) / 2.70
        alpha = d["alpha"] * vis * near * (0.48 + 0.78 * light) * 1.95
        alpha *= 0.90 + 0.18 * np.sin(3.0 * th + d["ph3"])

        # nearer particles are a little brighter and larger; far ones recede
        size = d["size"] * (0.62 + 0.40 * persp)
        colors = d["color"] * (0.58 + 0.62 * light)[:, None]
        colors = np.clip(colors, 0.0, 1.0)

        # three stamp classes keep the fine grain crisp while larger motes glow
        m3 = size <= 1.28
        m5 = size > 1.28
        m7 = size > 2.20

        for mask, kernel, mult in ((m3, K5B, 0.58), (m5, K7B, 0.34), (m7, K7B, 0.22)):
            if np.any(mask):
                self._deposit(xs[mask], ys[mask], alpha[mask] * mult,
                              colors[mask], kernel)

        # a soft skirt around every mote: this is what keeps the cloud luminous
        # when the video is viewed downscaled in a hero section
        self._deposit(xs, ys, alpha * 0.10, colors, K5B)

        # a restrained number of brighter seed points
        bright = (np.sin(5.0 * th + d["ph2"] * 1.7) > 0.72) & (r0 > 0.35)
        if np.any(bright):
            self._deposit(xs[bright], ys[bright], alpha[bright] * 0.60,
                          np.clip(colors[bright] * 1.10 + 0.06, 0, 1), K5B)

    # ------------------------------------------------------------------

    def _draw_strands(self, th, breath):
        s = self.strands
        n = s["base"].shape[0]
        S = s["s"].size
        p = s["base"][:, None, :]
        ax = s["axis"][:, None, :]
        ang = (s["s"][None, :] * s["span"][:, None] +
               0.14 * np.sin(TAU * s["s"][None, :] * 2.0 + s["phase"][:, None] + th))
        ca, sa = np.cos(ang)[..., None], np.sin(ang)[..., None]
        dot = np.sum(ax * p, axis=2, keepdims=True)
        cross = np.cross(ax, p)
        pts = p * ca + cross * sa + ax * dot * (1.0 - ca)
        # global spin plus breathing
        spin = s["swirl"] * th + 0.08 * np.sin(th + s["phase"])
        cs, ss = np.cos(spin), np.sin(spin)
        x = pts[..., 0] * cs[:, None] + pts[..., 2] * ss[:, None]
        z = -pts[..., 0] * ss[:, None] + pts[..., 2] * cs[:, None]
        y = pts[..., 1]
        rad = s["r"][:, None] * breath * (1.0 + 0.018 * np.sin(3.0 * np.pi * s["s"][None, :] + s["phase"][:, None]))
        x, y, z = x * self.R * rad, y * self.R * rad, z * self.R * rad
        xs, ys, persp = self._project(x.reshape(-1), y.reshape(-1), z.reshape(-1))
        depth = np.clip(z.reshape(-1) / self.R, -1.05, 1.05)
        near = 0.35 + 0.65 * (depth + 1.05) / 2.1
        alpha = s["alpha"][:, None] * near.reshape(n, S) * (0.84 + 0.16 * np.sin(
            2.0 * th + s["phase"][:, None] + 5.0 * s["s"][None, :]))
        colors = np.clip(s["color"][:, None, :] * (0.88 + 0.16 * persp.reshape(n, S)[..., None]), 0, 1)
        self._deposit(xs, ys, alpha.reshape(-1), colors.reshape(-1, 3), K3B)
        # a faint halo on alternating strands keeps the silk from looking wiry
        halo = np.arange(n) % 3 == 0
        self._deposit(xs.reshape(n, S)[halo].reshape(-1), ys.reshape(n, S)[halo].reshape(-1),
                      alpha[halo].reshape(-1) * 0.11,
                      colors[halo].reshape(-1, 3), K5B)

    def _draw_core_knot(self, th, breath):
        s = self.core_knot
        n = s["base"].shape[0]
        S = s["s"].size
        p = s["base"][:, None, :]
        ax = s["axis"][:, None, :]
        ang = (s["s"][None, :] * s["span"][:, None] +
               0.55 * np.sin(TAU * s["s"][None, :] * 1.5 + s["phase"][:, None] + th))
        ca, sa = np.cos(ang)[..., None], np.sin(ang)[..., None]
        dot = np.sum(ax * p, axis=2, keepdims=True)
        pts = p * ca + np.cross(ax, p) * sa + ax * dot * (1.0 - ca)
        spin = th + 0.20 * np.sin(th + s["phase"])
        cs, ss = np.cos(spin), np.sin(spin)
        x = pts[..., 0] * cs[:, None] + pts[..., 2] * ss[:, None]
        z = -pts[..., 0] * ss[:, None] + pts[..., 2] * cs[:, None]
        y = pts[..., 1]
        rad = s["r"][:, None] * breath
        x, y, z = x * self.R * rad, y * self.R * rad, z * self.R * rad
        xs, ys, persp = self._project(x.reshape(-1), y.reshape(-1), z.reshape(-1))
        alpha = s["alpha"][:, None] * (0.82 + 0.18 * np.sin(
            4.0 * th + s["phase"][:, None] + 8.0 * s["s"][None, :]))
        colors = np.clip(s["color"][:, None, :] * (0.94 + 0.12 * persp.reshape(n, S)[..., None]), 0, 1)
        self._deposit(xs, ys, alpha.reshape(-1) * 1.35, colors.reshape(-1, 3), K3B, top=True)
        self._deposit(xs, ys, alpha.reshape(-1) * 0.20, colors.reshape(-1, 3), K5B, top=True)
        self._deposit(xs.reshape(n, S)[::4].reshape(-1), ys.reshape(n, S)[::4].reshape(-1),
                      alpha[::4].reshape(-1) * 0.05,
                      np.array([[1.0, 0.50, 0.06]], dtype=np.float32), K11, top=True)

    # ------------------------------------------------------------------

    def _draw_streams(self, th, breath):
        s = self.streams
        angle = s["swirl"] * th + 0.09 * np.sin(th + s["phase"])
        x, y, z = self._rotate(s["x0"], s["y0"], s["z0"], angle,
                               0.035 * np.sin(th + s["beat"]))
        scale = breath * (1.0 + 0.018 * np.sin(2.0 * th + s["phase"]))
        x, y, z = x * scale, y * scale, z * scale
        xs, ys, persp = self._project(x, y, z)
        depth = np.clip(z / self.R, -1.2, 1.2)
        near = 0.30 + 0.74 * (depth + 1.2) / 2.4
        light = np.clip(0.55 - 0.18 * x / self.R + 0.30 * y / self.R +
                        0.30 * z / self.R, 0, 1)
        alpha = s["alpha"] * near * (0.82 + 0.26 * light)
        col = np.clip(s["color"] * (0.88 + 0.18 * near)[:, None], 0, 1)
        size = s["size"] * (0.76 + 0.22 * persp)

        m3 = size <= 1.35
        m5 = size > 1.35
        if np.any(m3):
            self._deposit(xs[m3], ys[m3], alpha[m3], col[m3], K3B)
        if np.any(m5):
            self._deposit(xs[m5], ys[m5], alpha[m5] * 0.82, col[m5], K5B)
        self._deposit(xs, ys, alpha * 0.12, col, K5B)
        glow = alpha > 0.56
        if np.any(glow):
            self._deposit(xs[glow], ys[glow], alpha[glow] * 0.06,
                          np.array([[1.0, 0.57, 0.10]], dtype=np.float32), K7B)

    def _draw_filaments(self, th, breath):
        f = self.filaments
        base = f["base"] * self.R
        n, L, _ = base.shape
        angle = f["swirl"] * th
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        x = base[..., 0] * cos_a[:, None] + base[..., 2] * sin_a[:, None]
        z = -base[..., 0] * sin_a[:, None] + base[..., 2] * cos_a[:, None]
        y = base[..., 1]
        # breathing stretch of the threads
        stretch = (0.995 + 0.030 * np.sin(th + f["beat"]))[:, None]
        x, y, z = x * breath * stretch, y * breath * stretch, z * breath * stretch

        xs, ys, persp = self._project(x, y, z)
        u = f["u"][None, :]
        # travelling pulse and a soft, uneven brightness along the thread
        pulse_pos = (f["speed"][:, None] * th / TAU + f["phase"][:, None] / TAU) % 1.0
        du = np.abs((u / TAU - pulse_pos + 0.5) % 1.0 - 0.5)
        pulse = np.exp(-(du ** 2) / (2 * 0.075 ** 2))
        along = (0.55 + 0.45 * np.sin(3.0 * u + f["beat"][:, None])) ** 1.6
        w = f["alpha"][:, None] * (0.55 + 0.50 * along + 0.70 * pulse)
        cols = f["color"][:, None, :] * (0.90 + 0.10 * persp[..., None])

        xf = xs.reshape(-1)
        yf = ys.reshape(-1)
        wf = (w * (0.8 + 0.2 * np.clip(persp, 0.8, 1.2))).reshape(-1)
        cf = cols.reshape(-1, 3)
        # a soft, continuous glow plus a crisp beaded core
        self._deposit(xf, yf, wf * 0.30, cf, K5B)
        self._deposit(xs[:, ::2].reshape(-1), ys[:, ::2].reshape(-1),
                      (wf.reshape(n, L)[:, ::2].reshape(-1) * 0.86),
                      cf.reshape(n, L, 3)[:, ::2].reshape(-1, 3), K3B)

        # brighter beads strung along the silk
        bead = (np.sin(9.0 * u + f["beat"][:, None]) > 0.80) & (pulse > 0.15)
        if np.any(bead):
            self._deposit(xs[bead], ys[bead], (wf.reshape(n, L)[bead] * 0.35),
                          np.clip(cf.reshape(n, L, 3)[bead] * 1.10 + 0.03, 0, 1), K5B)

    # ------------------------------------------------------------------

    def _conn_points(self, th, breath, i):
        c = self.connections
        p0 = c["d_start"][i] * self.R * (0.975 + 0.012 * math.sin(th + c["phase"][i]))
        dist = c["dist"][i] * (1.0 + 0.028 * math.sin(th + c["phase"][i] * 1.3))
        p3 = c["d_end"][i] * self.R * dist
        # endpoints drift very slightly, without moving the camera
        p3 = p3 + c["side"][i] * self.R * c["wide"][i] * 0.12 * math.sin(th + c["phase"][i])

        mid = 0.5 * (p0 + p3)
        p1 = p0 + (p3 - p0) * 0.30 + c["side"][i] * self.R * c["wide"][i]
        p2 = p0 + (p3 - p0) * 0.68 + c["side"][i] * self.R * c["wide"][i] * 0.55
        p1 = p1 + c["lift"][i] * self.R * 0.035 * math.sin(th + c["phase"][i])
        p2 = p2 - c["lift"][i] * self.R * 0.025 * math.cos(th + c["phase"][i])
        return p0, p1, p2, p3

    def _bezier(self, p0, p1, p2, p3, s):
        s = s[:, None]
        om = 1.0 - s
        return (om ** 3) * p0 + 3.0 * (om ** 2) * s * p1 + \
               3.0 * om * (s ** 2) * p2 + (s ** 3) * p3

    def _draw_connections(self, th, breath):
        c = self.connections
        n = c["d_start"].shape[0]
        s = np.linspace(0.0, 1.0, 190, dtype=np.float32)

        for i in range(n):
            p0, p1, p2, p3 = self._conn_points(th, breath, i)
            pts = self._bezier(p0, p1, p2, p3, s)
            # small perpendicular waviness
            pts = pts + c["side"][i] * self.R * 0.014 * np.sin(5.0 * np.pi * s + c["phase"][i])[:, None]
            xs, ys, persp = self._project(pts[:, 0], pts[:, 1], pts[:, 2])
            env = 0.22 + 0.78 * (s ** 0.45)
            pulse_pos = (c["speed"][i] * th / TAU * 1.0 + c["phase"][i] / TAU) % 1.0
            du = np.abs((s - pulse_pos + 0.5) % 1.0 - 0.5)
            pulse = np.exp(-(du ** 2) / (2 * 0.035 ** 2))
            w = c["alpha"][i] * env * (0.72 + 0.85 * pulse)
            col = c["color"][i][None, :] * (0.86 + 0.14 * persp[:, None])
            self._deposit(xs, ys, w * 0.90, np.clip(col, 0, 1), K3B)
            self._deposit(xs, ys, w * 0.17, np.clip(col, 0, 1), K5B)

            # a very faint halo, only on a sparse subset of samples
            hal = np.arange(0, s.size, 3)
            self._deposit(xs[hal], ys[hal], w[hal] * 0.20,
                          np.clip(col[hal], 0, 1), K5B)
            h11 = np.arange(0, s.size, 5)
            self._deposit(xs[h11], ys[h11], w[h11] * 0.075,
                          np.array([1.0, 0.40, 0.045], dtype=np.float32), K11)

            # terminal virtual node
            self._draw_node(p3, c["phase"][i], c["speed"][i], th, c["color"][i])

        # data packets
        for (i, off, speed, amp) in self.packets:
            p0, p1, p2, p3 = self._conn_points(th, breath, i)
            sp = (off + speed * th / TAU) % 1.0
            for k in range(9):
                ss = sp - 0.010 * k
                if ss < 0.03:
                    continue
                q = self._bezier(p0, p1, p2, p3, np.array([ss], dtype=np.float32))[0]
                qx, qy, _ = self._project(q[0], q[1], q[2])
                a = amp * math.exp(-k / 2.6) * min(1.0, (ss - 0.03) / 0.06)
                col = np.array([1.0, 0.79, 0.39], dtype=np.float32) * (0.8 + 0.2 * (1 - k / 9))
                self._deposit(np.array([qx]), np.array([qy]), np.array([a * 1.9]),
                              col[None, :], K3B if k > 2 else K5B, top=True)
            # tiny hot core + halo
            q = self._bezier(p0, p1, p2, p3, np.array([sp], dtype=np.float32))[0]
            qx, qy, _ = self._project(q[0], q[1], q[2])
            self._deposit(np.array([qx]), np.array([qy]), np.array([2.20]),
                          np.array([[1.0, 0.90, 0.60]], dtype=np.float32), K5B, top=True)
            self._deposit(np.array([qx]), np.array([qy]), np.array([0.30]),
                          np.array([[1.0, 0.62, 0.14]], dtype=np.float32), K11, top=True)

    def _draw_node(self, p, phase, speed, th, color):
        pulse = 0.62 + 0.38 * math.sin(th * 2.0 + phase)
        x, y, _ = self._project(p[0], p[1], p[2])
        self._deposit(np.array([x]), np.array([y]), np.array([4.20 * pulse]),
                      np.array([[1.0, 0.34, 0.015]], dtype=np.float32), K7B, top=True)
        self._deposit(np.array([x]), np.array([y]), np.array([2.00 * pulse]),
                      np.array([[1.0, 0.50, 0.035]], dtype=np.float32), K11, top=True)
        self._deposit(np.array([x]), np.array([y]), np.array([0.55 * pulse]),
                      np.array([[1.0, 0.42, 0.020]], dtype=np.float32), K15, top=True)

    # ------------------------------------------------------------------

    def _draw_orbiters(self, th, breath):
        o = self.orbit
        n = o["radius"].size
        a = o["speed"] * th + o["phase"]
        ca, sa = np.cos(a), np.sin(a)
        x = (o["x0"] * ca[:, None] + o["y0"] * sa[:, None])[:, 0]
        y = (o["x0"] * ca[:, None] + o["y0"] * sa[:, None])[:, 1]
        z = (o["x0"] * ca[:, None] + o["y0"] * sa[:, None])[:, 2]

        # add a gentle latitude wobble
        wob = o["wob"] * np.sin(3.0 * th + o["ph2"])
        y = y + wob
        xyz = unit(np.stack([x, y, z], axis=1), axis=1) * (o["radius"] * breath * self.R)[:, None]
        x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]

        # a short motion streak: the same particles a fraction of a frame earlier
        for lag, wm in ((0.012, 0.52), (0.024, 0.34), (0.036, 0.20), (0.048, 0.10)):
            aa = o["speed"] * (th - lag) + o["phase"]
            ca2, sa2 = np.cos(aa), np.sin(aa)
            x2 = (o["x0"] * ca2[:, None] + o["y0"] * sa2[:, None])[:, 0]
            y2 = (o["x0"] * ca2[:, None] + o["y0"] * sa2[:, None])[:, 1]
            z2 = (o["x0"] * ca2[:, None] + o["y0"] * sa2[:, None])[:, 2]
            xyz2 = unit(np.stack([x2, y2, z2], axis=1), axis=1) * (o["radius"] * breath * self.R)[:, None]
            xs2, ys2, _ = self._project(xyz2[:, 0], xyz2[:, 1], xyz2[:, 2])
            self._deposit(xs2, ys2, o["alpha"] * wm * 1.45, o["color"] * 0.85, K3B, top=True)

        xs, ys, persp = self._project(x, y, z)
        alpha = o["alpha"] * (0.82 + 0.22 * persp) * 1.55
        self._deposit(xs, ys, alpha, o["color"], K3B, top=True)
        # bright center + soft bloom on the rare larger orbiters
        self._deposit(xs, ys, alpha * 0.24,
                      np.clip(o["color"] * 1.15 + 0.08, 0, 1), K5B, top=True)
        halo = o["size"] > 2.4
        if np.any(halo):
            self._deposit(xs[halo], ys[halo], alpha[halo] * 0.10,
                          np.array([[1.0, 0.64, 0.20]], dtype=np.float32), K7, top=True)

    def _draw_core_sparks(self, th):
        s = self.core_sparks
        angle = th + 0.13 * np.sin(th + s["phase"])
        x, y, z = self._rotate(s["x0"] * self.R, s["y0"] * self.R,
                               s["z0"] * self.R, angle, 0.05)
        xs, ys, persp = self._project(x, y, z)
        tw = 0.72 + 0.28 * np.sin(3.0 * th + s["phase"])
        alpha = s["alpha"] * tw
        self._deposit(xs, ys, alpha * 1.55, s["color"], K3B, top=True)
        big = alpha > 0.82
        if np.any(big):
            self._deposit(xs[big], ys[big], alpha[big] * 0.34,
                          np.clip(s["color"][big] * 1.10 + 0.04, 0, 1), K5B, top=True)
            self._deposit(xs[big], ys[big], alpha[big] * 0.06,
                          np.array([[1.0, 0.55, 0.08]], dtype=np.float32), K11, top=True)

    def _draw_glints(self, th):
        g = self.glint
        n = g["radius"].size
        tw = (0.5 + 0.5 * np.sin(g["freq"] * th + g["phase"])) ** 4
        angle = th
        x, y, z = self._rotate(g["dir"][:, 0] * g["radius"] * self.R,
                               g["dir"][:, 1] * g["radius"] * self.R,
                               g["dir"][:, 2] * g["radius"] * self.R, angle, 0.03)
        xs, ys, _ = self._project(x, y, z)
        m = tw > 0.025
        if np.any(m):
            self._deposit(xs[m], ys[m], g["alpha"][m] * tw[m] * 1.8,
                          np.clip(g["color"][m] * (0.9 + 0.15 * tw[m, None]), 0, 1),
                          K5, top=True)
            stronger = m & (tw > 0.55)
            if np.any(stronger):
                self._deposit(xs[stronger], ys[stronger], g["alpha"][stronger] * tw[stronger] * 0.55,
                              np.array([[1.0, 0.86, 0.50]], dtype=np.float32), K7, top=True)

    def _draw_sparkles(self, th, breath):
        s = self.sparkles
        n = s["phase"].size
        tw = (0.5 + 0.5 * np.sin(s["freq"] * th + s["phase"])) ** 7

        angle = th * 0.0 + 0.10 * np.sin(th + s["phase"])
        x = (s["x0"] * breath + s["sway"] * np.sin(2.0 * th + s["phase"])) * self.R
        y = (s["y0"] * breath + s["sway"] * np.cos(3.0 * th + s["phase"])) * self.R
        z = s["z0"] * breath * self.R
        x, y, z = self._rotate(x, y, z, th, 0.04)
        xs, ys, _ = self._project(x, y, z)

        m = tw > 0.015
        if not np.any(m):
            return
        al = 0.50 * tw[m] * 1.7
        self._deposit(xs[m], ys[m], al, s["color"][m], K3B, top=True)
        # four-point glint around the brightest sparkles
        big = m & (tw > 0.60)
        if np.any(big):
            for dx, dy, ww in ((3.2, 0, 0.22), (-3.2, 0, 0.22), (0, 3.2, 0.22), (0, -3.2, 0.22)):
                self._deposit(xs[big] + dx * self.s, ys[big] + dy * self.s,
                              al[tw[m] > 0.60] * ww,
                              np.array([[1.0, 0.84, 0.46]], dtype=np.float32), K3B, top=True)

    # ------------------------------------------------------------------

    def _composite(self, th, breath):
        out = self.bg.copy()

        # volume tint first
        ha = self.haze_alpha
        if ha is not None and np.max(ha) > 0:
            out = out * (1.0 - ha[..., None]) + self.haze_rgb * ha[..., None]

        # a low, warm core glow, underneath the grain of the node
        dx = (self.xg - self.cx) / max(self.R, 1e-6)
        dy = (self.yg - self.cy) / max(self.R, 1e-6)
        dd = np.sqrt(dx * dx + dy * dy)
        core_a = (0.140 * np.exp(-(dd / 0.56) ** 2) +
                  0.100 * np.exp(-(dd / 0.22) ** 2))
        core_a = np.clip(core_a * (0.9 + 0.1 * math.sin(th)), 0.0, 0.24)
        core_col = np.array([1.0, 0.20, 0.018], dtype=np.float32)
        out = out * (1.0 - core_a[..., None]) + core_col * core_a[..., None]

        # the accumulated particles, filaments and links
        a_sum = self.A
        cov = 1.0 - np.exp(-a_sum * 1.10)
        col = self.E / (a_sum[..., None] + 1e-5)
        col = np.clip(col, 0.0, 1.0)
        out = out * (1.0 - cov[..., None]) + col * cov[..., None]

        # a denser, more saturated pigment in the body.  White stays white
        # because this is gated by coverage, but the reds and oranges gain the
        # weight they lose to the white background in plain alpha compositing.
        ink = cov[..., None] * 0.58 * (1.0 - col)
        out = out * np.exp(-ink)

        # a very soft bloom, built from the vivid premultiplied core only
        lum = 0.33 * col[..., 0] + 0.48 * col[..., 1] + 0.19 * col[..., 2]
        bright = np.clip((lum * cov - 0.24) / 0.76, 0.0, 1.0) * cov
        bright = gaussian_filter(bright, sigma=max(2.0, 12.0 * self.s))
        bcol = gaussian_filter(col * cov[..., None], sigma=(max(2.0, 12.0 * self.s),
                                                            max(2.0, 12.0 * self.s), 0))
        bcol = bcol / (gaussian_filter(cov, sigma=max(2.0, 12.0 * self.s))[..., None] + 1e-4)
        bcol = np.clip(bcol, 0.0, 1.0)
        ba = np.clip(bright * 0.48, 0.0, 0.28)
        out = out * (1.0 - ba[..., None]) + bcol * ba[..., None]

        # deepen the midtones so the warm body keeps its weight when the video
        # is displayed smaller than 1920 px. White stays white.
        out = np.power(np.clip(out, 0.0, 1.0), 1.22)

        # isolated bright motes (surface arcs, packets, nodes, glints) are
        # composited last; this keeps them legible even where the body is dense
        ta = self.topA
        tcov = 1.0 - np.exp(-ta * 1.15)
        tcol = np.clip(self.topE / (ta[..., None] + 1e-5), 0.0, 1.0)
        out = out * (1.0 - tcov[..., None]) + tcol * tcov[..., None]

        # a restrained chroma lift keeps the reds alive after H.264's
        # 4:2:0 chroma subsampling, without touching the white field
        lum = 0.299 * out[..., 0] + 0.587 * out[..., 1] + 0.114 * out[..., 2]
        out = lum[..., None] + (out - lum[..., None]) * 1.12

        # fine, static grain prevents gradient banding after H.264 encoding.
        # The pattern is fixed, so the loop stays clean.
        if not hasattr(self, "_grain"):
            gy, gx = np.mgrid[0:self.H, 0:self.W]
            rng = np.random.default_rng(1234)
            self._grain = rng.normal(0.0, 1.0, (self.H, self.W)).astype(np.float32)
        out = out + (self._grain * 0.0016)[..., None]
        return np.clip(out, 0.0, 1.0)


# ---------------------------------------------------------------------------


def save_preview(renderer, frames, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    for idx in frames:
        t0 = time.time()
        img = renderer.render(idx, 360)
        p = outdir / f"preview_{idx:03d}.png"
        Image.fromarray(img).save(p)
        print(f"preview {idx:03d} -> {p}  ({time.time() - t0:.2f}s)", flush=True)


def render_video(renderer, out_path: Path, fps: int, frames: int, crf: int,
                 preview_every: int = 0):
    import imageio_ffmpeg

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{renderer.W}x{renderer.H}", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
        "-profile:v", "high", "-tune", "animation",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    t_start = time.time()
    try:
        for i in range(frames):
            t0 = time.time()
            frame = renderer.render(i, frames)
            proc.stdin.write(frame.tobytes())
            if i % 10 == 0 or i == frames - 1:
                elapsed = time.time() - t_start
                eta = elapsed / (i + 1) * (frames - i - 1)
                print(f"frame {i + 1:4d}/{frames}  {time.time() - t0:.2f}s  "
                      f"elapsed {elapsed:6.1f}s  eta {eta:6.1f}s", flush=True)
            if preview_every and i % preview_every == 0:
                Image.fromarray(frame).save(out_path.with_name(
                    f"{out_path.stem}_f{i:04d}.png"))
    finally:
        proc.stdin.close()
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg exited with {rc}")
    print(f"wrote {out_path} in {time.time() - t_start:.1f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["preview", "video", "check-loop"])
    ap.add_argument("--out", default="ether-node-loop.mp4")
    ap.add_argument("--w", type=int, default=BASE_W)
    ap.add_argument("--h", type=int, default=BASE_H)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--frames", default="0,90,180,270")
    ap.add_argument("--crf", type=int, default=14)
    ap.add_argument("--preview-every", type=int, default=0)
    args = ap.parse_args()

    frames = int(round(DURATION * args.fps))
    r = EtherNode(args.w, args.h)

    if args.mode == "preview":
        save_preview(r, [int(x) for x in args.frames.split(",") if x != ""],
                     Path("previews"))
    elif args.mode == "check-loop":
        a = r.render(0, frames).astype(np.float32)
        b = r.render(frames, frames).astype(np.float32)
        diff = np.abs(a - b)
        print("max diff", diff.max(), "mean diff", diff.mean(),
              "psnr", 10 * np.log10(255.0 ** 2 / max(1e-9, np.mean(diff ** 2))))
        Image.fromarray(np.abs(a - b).clip(0, 255).astype(np.uint8)).save("loop_diff.png")
    else:
        render_video(r, Path(args.out), args.fps, frames, args.crf,
                     args.preview_every)


if __name__ == "__main__":
    main()
