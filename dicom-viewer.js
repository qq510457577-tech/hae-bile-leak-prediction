/**
 * 浏览器端原生DICOM查看器 (v2 - Fixed)
 *
 * 交互：
 *   左键拖拽（上下）= 亮度（窗位 WC）
 *   左键拖拽（左右）= 对比度（窗宽 WW）
 *   滚轮 = 缩放（以鼠标位置为中心）
 *   右键单击（不移动）= CT值测量（HU），自动复制到剪贴板
 *   右键拖拽（移动）= 距离测量（mm）
 */
class DicomCanvas {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');

    // ── DICOM 原始数据 ──
    this.rawPixels = null;   // Int16Array 或 Uint8Array，原始像素值（未加窗）
    this.imgW = 0;
    this.imgH = 0;
    this.wc = 40;
    this.ww = 400;
    this.slope = 1;
    this.intercept = 0;
    this.spacing = null;     // mm/pixel（像素间距）
    this.bitsStored = 16;

    // ── 显示状态 ──
    this.zoom = 1;
    this.panX = 0;           // 平移（像素，canvas 像素空间）
    this.panY = 0;

    // ── 离屏缓存 ──
    this.offscreen = null;   // OffscreenCanvas，存储窗口化后的图像
    this.cacheDirty = true;

    // ── 交互状态 ──
    this.dragging = false;
    this.dragBtn = -1;       // 0=左, 2=右
    this.dragX = 0;
    this.dragY = 0;
    this.dragStartX = 0;
    this.dragStartY = 0;
    this.dragMoved = false;

    // 鼠标悬停跟踪（用于滚轮缩放中心）
    this._hoverX = 0;
    this._hoverY = 0;

    // 右键测量
    this.measurePt = null;   // {x, y} 起始点
    this.measureCur = null;  // {x, y} 当前点/结束点
    this.measureActive = false;

    // ── 绑定事件 ──
    this._binds();
  }

  _binds() {
    this._onDown = (e) => this._down(e);
    this._onMove = (e) => this._move(e);
    this._onUp = (e) => this._up(e);
    this._onWheel = (e) => this._wheel(e);
    this._onResize = () => this._resize();
    this._onCtx = (e) => e.preventDefault();

    this.canvas.addEventListener('pointerdown', this._onDown);
    this.canvas.addEventListener('pointermove', this._onMove);
    this.canvas.addEventListener('pointerup', this._onUp);
    this.canvas.addEventListener('pointerleave', this._onUp);
    this.canvas.addEventListener('wheel', this._onWheel, { passive: false });
    this.canvas.addEventListener('contextmenu', this._onCtx);
    window.addEventListener('resize', this._onResize);
  }

  // ── 坐标转换 ──

  /** CSS 像素 → Canvas 设备像素 */
  _cssX(clientX) {
    const rect = this.canvas.getBoundingClientRect();
    return (clientX - rect.left) / rect.width * this.canvas.width;
  }
  _cssY(clientY) {
    const rect = this.canvas.getBoundingClientRect();
    return (clientY - rect.top) / rect.height * this.canvas.height;
  }

  /** Canvas 设备像素 → 图像像素坐标（未缩放未平移的原始图像坐标系） */
  _toImage(cx, cy) {
    const w = this.canvas.width;
    const h = this.canvas.height;
    return {
      x: (cx - w / 2 - this.panX) / this.zoom + w / 2,
      y: (cy - h / 2 - this.panY) / this.zoom + h / 2,
    };
  }

  /** 图像像素坐标 → DICOM 像素索引 */
  _imgToDicom(imgX, imgY) {
    const fx = Math.round(imgX / this.canvas.width * this.imgW);
    const fy = Math.round(imgY / this.canvas.height * this.imgH);
    return { fx, fy, idx: fy * this.imgW + fx };
  }

  /** CSS client 坐标 → 归一化 DICOM 像素 (0..1) */
  _clientToNorm(clientX, clientY) {
    const cx = this._cssX(clientX);
    const cy = this._cssY(clientY);
    const img = this._toImage(cx, cy);
    return { nx: img.x / this.canvas.width, ny: img.y / this.canvas.height };
  }

  // ── 加载 DICOM ──

  async load(url) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
      const buf = await resp.arrayBuffer();
      const ds = dicomParser.parseDicom(new Uint8Array(buf));

      try {
        const ps = ds.string('x00280030');
        if (ps) this.spacing = parseFloat(ps.split('\\')[0]);
      } catch (e) {}
      try {
        const cc = ds.string('x00281050');
        if (cc) this.wc = parseFloat(cc.split('\\')[0]);
      } catch (e) {}
      try {
        const cw = ds.string('x00281051');
        if (cw) this.ww = parseFloat(cw.split('\\')[0]);
      } catch (e) {}
      try { this.slope = parseFloat(ds.string('x00281053') || '1'); } catch (e) {}
      try { this.intercept = parseFloat(ds.string('x00281052') || '0'); } catch (e) {}

      this.imgW = ds.uint16('x00280011');
      this.imgH = ds.uint16('x00280010');
      this.bitsStored = ds.uint16('x00280101') || 16;

      // 读取像素表示（0=unsigned, 1=signed）
      let pixelRep = 0;
      try { pixelRep = ds.uint16('x00280103'); } catch (e) {}
      this.pixelRep = pixelRep;

      const pixelBlock = ds.getPixelData ? ds.getPixelData() : (() => {
        const el = ds.elements.x7fe00010;
        if (!el) throw new Error('No pixel data element found');
        return new Uint8Array(
          ds.byteArray.buffer,
          ds.byteArray.byteOffset + el.dataOffset,
          el.length
        );
      })();
      if (this.bitsStored > 8) {
        this.rawPixels = new Int16Array(this.imgW * this.imgH);
        const view = new DataView(pixelBlock.buffer, pixelBlock.byteOffset, pixelBlock.byteLength);
        for (let i = 0; i < this.imgW * this.imgH; i++) {
          if (pixelRep === 1) {
            this.rawPixels[i] = view.getInt16(i * 2, true);
          } else {
            this.rawPixels[i] = view.getUint16(i * 2, true);
          }
        }
      } else {
        this.rawPixels = new Uint8Array(pixelBlock);
      }

      this.zoom = 1;
      this.panX = 0;
      this.panY = 0;
      this.measurePt = null;
      this.measureCur = null;
      this.measureActive = false;
      this.cacheDirty = true;

      this._resize();
      this.render();
    } catch (e) {
      console.error('DICOM加载失败:', e.message);
      const c = this.ctx;
      const w = this.canvas.width || 400;
      const h = this.canvas.height || 400;
      c.fillStyle = '#111';
      c.fillRect(0, 0, w, h);
      c.fillStyle = '#ef4444';
      c.font = '14px sans-serif';
      c.textAlign = 'center';
      c.fillText('DICOM加载失败: ' + e.message, w/2, h/2);
    }
  }

  toHU(v) { return v * this.slope + this.intercept; }

  // ── 重建离屏缓存 ──

  _rebuildCache() {
    if (!this.rawPixels) return;
    const w = this.imgW;
    const h = this.imgH;
    // 离屏 canvas 使用 DICOM 原生尺寸，不缩放
    if (!this.offscreen || this.offscreen.width !== w || this.offscreen.height !== h) {
      if (typeof OffscreenCanvas !== 'undefined') {
        this.offscreen = new OffscreenCanvas(w, h);
      } else {
        this.offscreen = document.createElement('canvas');
        this.offscreen.width = w;
        this.offscreen.height = h;
      }
    }
    const octx = this.offscreen.getContext('2d');
    const low = this.wc - this.ww / 2;
    const high = this.wc + this.ww / 2;
    const range = high - low || 1;

    const imgData = octx.createImageData(w, h);
    const d = imgData.data;
    for (let i = 0; i < w * h; i++) {
      let hu = this.toHU(this.rawPixels[i]);
      let val = Math.max(0, Math.min(255, ((hu - low) / range) * 255));
      const pi = i * 4;
      d[pi] = val;
      d[pi + 1] = val;
      d[pi + 2] = val;
      d[pi + 3] = 255;
    }
    octx.putImageData(imgData, 0, 0);
    this.cacheDirty = false;
  }

  // ── 渲染 ──

  render() {
    const c = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;

    if (!this.rawPixels) {
      c.fillStyle = '#000';
      c.fillRect(0, 0, w, h);
      return;
    }

    if (this.cacheDirty) this._rebuildCache();

    // 清空
    c.fillStyle = '#000';
    c.fillRect(0, 0, w, h);

    // 计算显示区域
    const srcW = w / this.zoom;
    const srcH = h / this.zoom;
    // 显示区域中心 = canvas 中心 - 平移
    const srcCX = w / 2 - this.panX / this.zoom;
    const srcCY = h / 2 - this.panY / this.zoom;
    const srcX = srcCX - srcW / 2;
    const srcY = srcCY - srcH / 2;

    // 将 canvas 像素坐标映射到离屏缓存（DICOM 原生尺寸）的坐标
    const scaleX = this.imgW / w;
    const scaleY = this.imgH / h;

    // drawImage 支持只取源图的一部分显示到目标
    // 目标位置 (0, 0, w, h)，源位置 (srcX * scaleX, srcY * scaleY, srcW * scaleX, srcH * scaleY)
    c.drawImage(
      this.offscreen,
      srcX * scaleX,
      srcY * scaleY,
      srcW * scaleX,
      srcH * scaleY,
      0,
      0,
      w,
      h
    );

    // ── 测量线 ──
    if (this.measurePt) {
      const p1 = this._toCanvasDevice(this.measurePt.x, this.measurePt.y);
      c.save();
      c.strokeStyle = '#22c55e';
      c.lineWidth = 2;
      c.setLineDash([6, 4]);

      if (this.measureCur && this.measureActive) {
        const p2 = this._toCanvasDevice(this.measureCur.x, this.measureCur.y);
        c.beginPath();
        c.moveTo(p1.x, p1.y);
        c.lineTo(p2.x, p2.y);
        c.stroke();
        c.setLineDash([]);

        // 距离标注
        const dx = (this.measureCur.x - this.measurePt.x) * (this.imgW / w);
        const dy = (this.measureCur.y - this.measurePt.y) * (this.imgH / h);
        const pixDist = Math.sqrt(dx * dx + dy * dy);
        const distText = this.spacing ? `${(pixDist * this.spacing).toFixed(1)} mm` : `${pixDist.toFixed(0)} px`;
        const mx = (p1.x + p2.x) / 2;
        const my = (p1.y + p2.y) / 2;
        c.font = 'bold 14px monospace';
        c.fillStyle = '#22c55e';
        const tw = c.measureText(distText).width;
        c.fillStyle = 'rgba(0,0,0,0.6)';
        c.fillRect(mx + 6 - 2, my - 10 - 2, tw + 4, 18);
        c.fillStyle = '#22c55e';
        c.fillText(distText, mx + 6, my - 10 + 12);

        // 端点圆点
        c.fillStyle = '#22c55e';
        c.beginPath();
        c.arc(p1.x, p1.y, 4, 0, Math.PI * 2);
        c.fill();
        c.beginPath();
        c.arc(p2.x, p2.y, 4, 0, Math.PI * 2);
        c.fill();
      } else {
        // 只有起点，画一个圆点
        c.setLineDash([]);
        c.fillStyle = '#22c55e';
        c.beginPath();
        c.arc(p1.x, p1.y, 4, 0, Math.PI * 2);
        c.fill();
      }
      c.restore();
    }

    // ── 信息叠加 ──
    c.save();
    c.font = '13px monospace';
    const infoLines = [
      `WC:${this.wc.toFixed(0)}  WW:${this.ww.toFixed(0)}  ${(this.zoom).toFixed(1)}x`,
    ];
    if (this.spacing) infoLines.push(`PS:${this.spacing}mm`);

    // 半透明背景
    c.fillStyle = 'rgba(0,0,0,0.55)';
    const lh = 18;
    c.fillRect(4, 4, 240, lh * infoLines.length + 4);

    c.fillStyle = 'rgba(255,255,255,0.8)';
    infoLines.forEach((line, i) => {
      c.fillText(line, 10, 14 + i * lh + lh);
    });

    // 左下角操作提示
    const tips = '左拖=窗宽窗位 · 滚轮=缩放 · 右单击=CT值 · 右拖=测距';
    c.fillStyle = 'rgba(255,255,255,0.35)';
    c.font = '11px sans-serif';
    c.fillText(tips, 10, h - 8);
    c.restore();
  }

  /** 图像坐标 → Canvas 设备坐标（含 zoom/pan） */
  _toCanvasDevice(imgX, imgY) {
    const w = this.canvas.width;
    const h = this.canvas.height;
    return {
      x: (imgX - w / 2) * this.zoom + w / 2 + this.panX,
      y: (imgY - h / 2) * this.zoom + h / 2 + this.panY,
    };
  }

  // ── Canvas 尺寸适配 ──

  _resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const newW = Math.round(rect.width * dpr);
    const newH = Math.round(rect.height * dpr);
    if (this.canvas.width !== newW || this.canvas.height !== newH) {
      this.canvas.width = newW;
      this.canvas.height = newH;
      // 保持 CSS 尺寸不变（由父容器控制）
      this.canvas.style.width = rect.width + 'px';
      this.canvas.style.height = rect.height + 'px';
      this.cacheDirty = true;
      this.render();
    }
  }

  // ── 事件处理 ──

  _down(e) {
    e.preventDefault();
    this.canvas.setPointerCapture(e.pointerId);
    this.dragging = true;
    this.dragBtn = e.button;
    this.dragX = e.clientX;
    this.dragY = e.clientY;
    this.dragStartX = e.clientX;
    this.dragStartY = e.clientY;
    this.dragMoved = false;

    if (e.button === 0) {
      // 左键：窗宽窗位模式
    } else if (e.button === 2) {
      // 右键：记录起始点
      const n = this._clientToNorm(e.clientX, e.clientY);
      this.measurePt = { x: n.nx * this.canvas.width, y: n.ny * this.canvas.height };
      this.measureCur = null;
      this.measureActive = false;
    }
    this.render();
  }

  _move(e) {
    // 始终跟踪鼠标在画布上的位置（用于滚轮缩放中心）
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this._hoverX = (e.clientX - rect.left) / rect.width * this.canvas.width;
    this._hoverY = (e.clientY - rect.top) / rect.height * this.canvas.height;

    if (!this.dragging) return;
    const dx = e.clientX - this.dragX;
    const dy = e.clientY - this.dragY;
    this.dragX = e.clientX;
    this.dragY = e.clientY;

    const totalDx = e.clientX - this.dragStartX;
    const totalDy = e.clientY - this.dragStartY;
    if (Math.sqrt(totalDx * totalDx + totalDy * totalDy) > 5) {
      this.dragMoved = true;
    }

    if (this.dragBtn === 0) {
      // 左键拖拽：窗宽窗位
      // 左右 = 窗宽，上下 = 窗位
      this.ww = Math.max(1, this.ww + dx * 0.5);
      this.wc = Math.max(-1024, Math.min(2048, this.wc - dy * 0.5));
      this.cacheDirty = true;
      this.render();
    } else if (this.dragBtn === 2 && this.dragMoved) {
      // 右键拖拽（已移动）：更新测量终点
      this.measureActive = true;
      const n = this._clientToNorm(e.clientX, e.clientY);
      this.measureCur = { x: n.nx * this.canvas.width, y: n.ny * this.canvas.height };
      this.render();
    }
  }

  _up(e) {
    this.dragging = false;
    try { this.canvas.releasePointerCapture(e.pointerId); } catch (ex) {}

    if (this.dragBtn === 2 && !this.dragMoved) {
      // 右键单击（没移动）：测量 CT 值
      this.measureActive = false;
      const n = this._clientToNorm(e.clientX, e.clientY);
      const fx = Math.round(n.nx * this.imgW);
      const fy = Math.round(n.ny * this.imgH);
      const idx = fy * this.imgW + fx;
      if (idx >= 0 && idx < this.rawPixels.length) {
        const hu = this.toHU(this.rawPixels[idx]);
        this._showTip(e.clientX, e.clientY, `CT值: ${hu.toFixed(0)} HU`);
        navigator.clipboard.writeText(`${hu.toFixed(0)} HU`).catch(() => {});
      }
      this.measurePt = null;
      this.measureCur = null;
      this.render();
    } else if (this.dragBtn === 2 && this.dragMoved) {
      // 右键拖拽完成：保留测量线（persist）
      this.measureActive = true;
      this.render();
    }
  }

  _wheel(e) {
    e.preventDefault();
    // 从鼠标悬停跟踪获取位置（滚动前的实时鼠标位置）
    const mx = this._hoverX;
    const my = this._hoverY;

    const oldZoom = this.zoom;
    const newZoom = Math.max(0.3, Math.min(20, oldZoom * (e.deltaY > 0 ? 0.9 : 1.1)));
    const factor = newZoom / oldZoom;

    // 调整平移，使鼠标悬停位置保持不动
    const w2 = this.canvas.width / 2;
    const h2 = this.canvas.height / 2;
    this.panX = this.panX * factor + (mx - w2) * (1 - factor);
    this.panY = this.panY * factor + (my - h2) * (1 - factor);
    this.zoom = newZoom;

    this.render();
  }

  // ── CT 值浮窗 ──

  _showTip(clientX, clientY, text) {
    const el = document.createElement('div');
    el.textContent = text;
    el.style.cssText =
      `position:fixed;left:${clientX + 14}px;top:${clientY - 32}px;` +
      `background:#1e293b;color:#22c55e;padding:4px 10px;border-radius:5px;` +
      `font:bold 14px monospace;z-index:9999;pointer-events:none;white-space:nowrap;` +
      `box-shadow:0 2px 8px rgba(0,0,0,0.3)`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2500);
  }

  destroy() {
    this.canvas.removeEventListener('pointerdown', this._onDown);
    this.canvas.removeEventListener('pointermove', this._onMove);
    this.canvas.removeEventListener('pointerup', this._onUp);
    this.canvas.removeEventListener('pointerleave', this._onUp);
    this.canvas.removeEventListener('wheel', this._onWheel);
    this.canvas.removeEventListener('contextmenu', this._onCtx);
    window.removeEventListener('resize', this._onResize);
  }
}
