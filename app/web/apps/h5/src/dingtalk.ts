// 钉钉 JSAPI 共享封装（免登 / 扫码）。同源打包 dingtalk-jsapi，不依赖外部 CDN。
import * as ddModule from "dingtalk-jsapi";
import { apiGet, apiPost } from "@fdc/api-client";

declare global {
  interface Window {
    dd?: any;
  }
}

export function inDingTalk(): boolean {
  return /DingTalk/i.test(navigator.userAgent);
}

export function corpIdFromUrl(): string {
  const fromSearch = new URLSearchParams(location.search);
  if (fromSearch.get("corpId") || fromSearch.get("corpid"))
    return (fromSearch.get("corpId") || fromSearch.get("corpid"))!;
  const hash = location.hash.includes("?") ? location.hash.split("?")[1] : "";
  const fromHash = new URLSearchParams(hash);
  return fromHash.get("corpId") || fromHash.get("corpid") || "";
}

function resolveDD(): any {
  const bundled: any = (ddModule as any).default ?? ddModule;
  return window.dd ?? bundled;
}

export function loadDD(): Promise<any> {
  return new Promise((resolve, reject) => {
    const dd = resolveDD();
    if (!dd) return reject(new Error("未找到钉钉 JSAPI（dingtalk-jsapi 未加载）"));
    try {
      if (typeof dd.error === "function") dd.error((err: unknown) => reject(err));
      if (typeof dd.ready === "function") {
        let settled = false;
        dd.ready(() => {
          settled = true;
          resolve(dd);
        });
        setTimeout(() => {
          if (!settled) resolve(dd);
        }, 1500);
      } else {
        resolve(dd);
      }
    } catch {
      resolve(dd);
    }
  });
}

export function requestAuthCode(dd: any, corpId: string): Promise<string> {
  return new Promise((resolve, reject) => {
    try {
      if (dd?.runtime?.permission?.requestAuthCode) {
        dd.runtime.permission.requestAuthCode({
          corpId,
          onSuccess: (info: any) => resolve(info.code),
          onFail: reject,
        });
      } else if (typeof dd?.requestAuthCode === "function") {
        dd.requestAuthCode({ corpId }).then((r: any) => resolve(r.code)).catch(reject);
      } else {
        reject(new Error("当前 SDK 不支持 requestAuthCode"));
      }
    } catch (e) {
      reject(e);
    }
  });
}

export function scanQRCode(dd: any): Promise<string> {
  return new Promise((resolve, reject) => {
    try {
      if (typeof dd?.scan === "function") {
        const ret: any = dd.scan({
          type: "qr",
          success: (res: any) => resolve(res?.text ?? ""),
          fail: reject,
        });
        if (ret && typeof ret.then === "function") {
          ret.then((res: any) => resolve(res?.text ?? "")).catch(reject);
        }
      } else if (dd?.biz?.util?.scan) {
        dd.biz.util.scan({
          type: "qrCode",
          onSuccess: (res: any) => resolve(res?.text ?? ""),
          onFail: reject,
        });
      } else {
        reject(new Error("当前 SDK 不支持扫码（dd.scan / dd.biz.util.scan 均不可用）"));
      }
    } catch (e) {
      reject(e);
    }
  });
}

export type DingtalkConfig = { corpId: string; agentId: string; configured: boolean };

export type DingtalkUser = {
  configured: boolean;
  authCode?: string;
  userid?: string;
  matched?: boolean;
  imported?: boolean;
  ddUserid?: string;
  resolvedUserid?: string;
  name?: string | null;
  mobile?: string | null;
  title?: string | null;
  deptIdList?: number[] | null;
  role?: string;
  userGetError?: { errcode?: number; errmsg?: string };
  hint?: string;
  message?: string;
  raw?: unknown;
};

export function getDingtalkConfig() {
  return apiGet<DingtalkConfig>("/dingtalk/config");
}

// 取一枚新的免登 authCode（authCode 一次性，每次调用都需重新获取）
async function freshAuthCode(onLog?: (msg: string) => void): Promise<string> {
  const log = (m: string) => onLog?.(m);
  const cfg = await getDingtalkConfig();
  log(
    `后端配置: corpId=${cfg.corpId || "(空)"} agentId=${cfg.agentId || "(空)"} 服务端凭据=${
      cfg.configured ? "已配置" : "未配置"
    }`
  );
  log("加载钉钉 JSAPI ...");
  const dd = await loadDD();
  log("JSAPI ready ✓");
  const corpId = cfg.corpId || corpIdFromUrl();
  log(`使用 corpId: ${corpId || "(空)"}`);
  log("请求免登 authCode ...");
  const code = await requestAuthCode(dd, corpId);
  log(`authCode: ${code} ✓`);
  return code;
}

// 完整免登流程：取配置 → ready → requestAuthCode → 后端换用户信息
export async function dingtalkLogin(onLog?: (msg: string) => void): Promise<DingtalkUser> {
  const code = await freshAuthCode(onLog);
  onLog?.("用 authCode 换取用户信息 ...");
  return apiPost<DingtalkUser>("/dingtalk/login", { authCode: code });
}
