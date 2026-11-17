import { useRef, useState } from "react";
import { Button, Modal, Space, message } from "antd";
import { QRCodeCanvas } from "@rc-component/qrcode";
import { buildScanPayload, type FurnaceItem } from "@fdc/api-client";

/** 单张二维码卡片的绘制尺寸（导出图片用） */
const QR = 220; // 二维码像素
const PAD = 20; // 卡片内边距
const LINE = 30; // 文字行高
const TEXT_LINES = 3; // 地区·工作点编号 / 设备类型 / 钉钉扫码提示
const TIP = "请使用钉钉工作台打开应用扫码";
const CARD_W = QR + PAD * 2;
const CARD_H = QR + PAD * 2 + LINE * TEXT_LINES + 6;
const COLS = 3; // 批量导出每行卡片数

/**
 * 工作点二维码导出：把选中工作点的二维码 + 中文信息（地区/工作点编号/设备类型，不含值班长）
 * 合成为一张 PNG 图片下载。单个=一张卡片，多选/全量=网格拼图。
 */
export default function FurnaceQrExport({
  furnaces,
  open,
  onClose,
  title,
}: {
  furnaces: FurnaceItem[];
  open: boolean;
  onClose: () => void;
  title: string;
}) {
  // 收集每个工作点二维码的 <canvas> 引用，下载时读取像素
  const canvasRefs = useRef<Record<number, HTMLCanvasElement | null>>({});
  const [downloading, setDownloading] = useState(false);

  const drawCard = (
    ctx: CanvasRenderingContext2D,
    f: FurnaceItem,
    qr: HTMLCanvasElement | null,
    x: number,
    y: number,
  ) => {
    // 卡片白底 + 边框
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(x, y, CARD_W, CARD_H);
    ctx.strokeStyle = "#e0e0e0";
    ctx.lineWidth = 1;
    ctx.strokeRect(x + 0.5, y + 0.5, CARD_W - 1, CARD_H - 1);
    // 二维码
    if (qr) ctx.drawImage(qr, x + PAD, y + PAD, QR, QR);
    // 中文信息（地区 / 工作点编号 / 设备类型）——不含值班长
    const tx = x + CARD_W / 2;
    let ty = y + PAD + QR + LINE;
    ctx.textAlign = "center";
    ctx.fillStyle = "#222";
    ctx.font = "bold 20px sans-serif";
    ctx.fillText(`${f.region} · ${f.furnaceNo}`, tx, ty);
    ty += LINE;
    ctx.fillStyle = "#666";
    ctx.font = "16px sans-serif";
    ctx.fillText(`设备类型：${f.furnaceType || "—"}`, tx, ty);
    ty += LINE;
    ctx.fillStyle = "#1677ff";
    ctx.font = "14px sans-serif";
    ctx.fillText(TIP, tx, ty);
  };

  const download = async () => {
    if (!furnaces.length) {
      message.warning("没有可导出的工作点");
      return;
    }
    setDownloading(true);
    try {
      // 等一帧确保二维码 canvas 已渲染完成
      await new Promise((r) => requestAnimationFrame(() => r(null)));
      const n = furnaces.length;
      const cols = Math.min(COLS, n);
      const rows = Math.ceil(n / cols);
      const gap = 16;
      const W = cols * CARD_W + (cols + 1) * gap;
      const H = rows * CARD_H + (rows + 1) * gap;
      const out = document.createElement("canvas");
      out.width = W;
      out.height = H;
      const ctx = out.getContext("2d");
      if (!ctx) throw new Error("canvas 不可用");
      ctx.fillStyle = "#f5f5f5";
      ctx.fillRect(0, 0, W, H);
      furnaces.forEach((f, i) => {
        const c = i % cols;
        const r = Math.floor(i / cols);
        const x = gap + c * (CARD_W + gap);
        const y = gap + r * (CARD_H + gap);
        drawCard(ctx, f, canvasRefs.current[f.id] ?? null, x, y);
      });
      const fname =
        n === 1
          ? `工作点二维码_${furnaces[0].region}_${furnaces[0].furnaceNo}.png`
          : `工作点二维码_${n}个.png`;
      const blob: Blob = await new Promise((res, rej) =>
        out.toBlob((b) => (b ? res(b) : rej(new Error("生成图片失败"))), "image/png"),
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      message.success(`已导出 ${n} 个工作点二维码`);
    } catch (e) {
      message.error((e as Error).message || "导出失败");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={title}
      width={760}
      footer={
        <Space>
          <Button onClick={onClose}>关闭</Button>
          <Button type="primary" loading={downloading} onClick={download} data-testid="qr-download">
            下载二维码图片（PNG）
          </Button>
        </Space>
      }
    >
      <div style={{ color: "#888", marginBottom: 12 }}>
        共 {furnaces.length} 个工作点。图片含地区 / 工作点编号 / 设备类型（不含值班长信息）。
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, maxHeight: 460, overflowY: "auto" }}>
        {furnaces.map((f) => (
          <div
            key={f.id}
            style={{
              width: CARD_W,
              padding: PAD,
              background: "#fff",
              border: "1px solid #e0e0e0",
              borderRadius: 8,
              textAlign: "center",
            }}
          >
            <QRCodeCanvas
              value={buildScanPayload(f.code ?? "")}
              size={QR}
              ref={(el: HTMLCanvasElement | null) => {
                canvasRefs.current[f.id] = el;
              }}
            />
            <div style={{ fontWeight: 700, fontSize: 16, marginTop: 8 }}>
              {f.region} · {f.furnaceNo}
            </div>
            <div style={{ color: "#666", fontSize: 13 }}>设备类型：{f.furnaceType || "—"}</div>
            <div style={{ color: "#1677ff", fontSize: 12, marginTop: 2 }}>{TIP}</div>
          </div>
        ))}
      </div>
    </Modal>
  );
}
