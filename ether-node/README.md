# Ether Node — 6 秒无缝循环粒子能量节点

一支为白色科技风 Hero 区域生成的抽象动态视频。画面由程序化粒子渲染而成，不依赖任何外部素材。

## 交付文件

- `ether-node-loop.mp4` — 1920×1080、60 fps、6.00 秒、H.264 High、约 22.9 MB、无缝循环
- `ether-node-poster.png` — 首帧海报（1920×1080）
- `render_ether_node.py` — 完整渲染源码（numpy + scipy + ffmpeg）

## 画面结构

- 中央球形粒子节点：数万颗粒子与细丝，内部密、边缘疏，带红→橙→金的径向配色。
- 球形呼吸：一个 6 秒完整周期，径向扩张约 5%，同时叠加每个粒子自己的相位漂移，避免机械式整体缩放。
- 表面流：15 组金色粒子弧线沿球面流动，带短拖尾。
- 能量丝线：150 条弯曲光纤在球体内交织；42 条深红/金色核心丝线围绕核心收紧。
- 神经网络连接：11 条从球面伸出的有机曲线，连向球外节点，带脉冲与流动数据光点。
- 光效：柔光粒子核、低强度 bloom、固定细颗粒噪点（抑制 H.264 渐变色带）。

## 循环实现

所有运动都写成 `θ = 2π·t/6` 的整数次谐波，或整数圈旋转；随机参数只在初始化时生成。因此第 360 帧与第 0 帧逐像素完全一致：

```bash
python render_ether_node.py check-loop --w 960 --h 540
# max diff 0.0
```

编码后的视频也做了边界帧检查：第 359 帧到第 0 帧的平均差异为 2.69/255，与普通相邻帧的 2.37/255 接近，说明循环处没有跳变。

## 重新渲染

```bash
python -m pip install numpy scipy pillow imageio-ffmpeg
python render_ether_node.py preview --w 1920 --h 1080 --frames 0,90,180
python render_ether_node.py video --w 1920 --h 1080 --fps 60 --out ether-node-loop.mp4 --crf 17
```

`--w / --h` 控制分辨率，`--fps` 控制帧率（6 秒时长固定），`--crf` 控制 H.264 质量。
