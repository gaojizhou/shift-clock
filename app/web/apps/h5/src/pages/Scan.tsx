import { useEffect, useMemo, useState } from "react";
import { Button, Dialog, DotLoading, NavBar, Popup, Selector, Toast } from "antd-mobile";
import { useNavigate, useSearchParams } from "react-router-dom";
import EmployeeBadge from "../components/EmployeeBadge";
import {
  apiGet,
  apiPost,
  getDemoContext,
  getFurnaces,
  getQuota,
  getSelectablePosts,
  getScanSuggest,
  parseScanPayload,
  SQUADS,
  type FurnaceItem,
} from "@fdc/api-client";
import { useAuth } from "../auth";

declare global {
  interface Window {
    __FDC_SCAN_DATE__?: string;
  }
}

function squadStorageKey(region: string, furnaceNo: string) {
  return `fdc_last_squad_${region}|${furnaceNo}`;
}

type PostQuota = {
  formalUsed: number;
  capacity: number;
  allowedEmployTypes: string[];
};

function squadForPost(furnaceItem: FurnaceItem | null, postName: string, squad: string | null) {
  if (!furnaceItem || !squad) return undefined;
  const rules = furnaceItem.quotaRules.filter((r) => r.post === postName);
  if (rules.length === 0 || rules.some((r) => r.squadless)) return undefined;
  return squad;
}

export default function ScanPage() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const { me } = useAuth();
  // 补录模式：带 date 参数（非当天）。同一套选工作点/班次/用工/半班/工种向导，提交走 /makeup（待值班长审核）。
  const makeupDate = params.get("date") || "";
  const isMakeup = !!makeupDate;
  const [reason, setReason] = useState("漏扫");
  const userid = me.userid || localStorage.getItem("fdc_userid") || "";
  const name = me.name || "";
  const [region, setRegion] = useState("");
  const [furnaceNo, setFurnaceNo] = useState("");
  const [furnaceType, setFurnaceType] = useState("");
  const [furnaceItem, setFurnaceItem] = useState<FurnaceItem | null>(null);
  const [squad, setSquad] = useState<string | null>(null);
  const [post, setPost] = useState("");
  const [posts, setPosts] = useState<string[]>([]);
  const [quotaByPost, setQuotaByPost] = useState<Record<string, PostQuota>>({});
  const [success, setSuccess] = useState("");
  const [employType, setEmployType] = useState("");
  const [scanDate, setScanDate] = useState(window.__FDC_SCAN_DATE__ || "");
  const [alreadyOn, setAlreadyOn] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [month, setMonth] = useState("2026-04");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [half, setHalf] = useState(false); // 是否半个班（正式/实习/待岗均可选）
  const [halfOpen, setHalfOpen] = useState(false); // 工时选择是否展开（默认收起；展开后不可收起，进页面重置）
  const [squadlessMode, setSquadlessMode] = useState(false); // 选了「不分班」轨道（值班长/维修工等不分班编制）

  useEffect(() => {
    let cancelled = false;
    if (userid) localStorage.setItem("fdc_userid", userid);
    if (isMakeup) {
      // 补录：日期/月份锚定到所选补录日
      setScanDate(makeupDate);
      setMonth(makeupDate.slice(0, 7));
    } else {
      getDemoContext()
        .then((c) => {
          if (cancelled) return;
          setScanDate(window.__FDC_SCAN_DATE__ || c.today);
          setMonth(c.month);
        })
        .catch(() => {
          if (!cancelled) setScanDate(window.__FDC_SCAN_DATE__ || "");
        });
    }
    return () => {
      cancelled = true;
    };
  }, [isMakeup, makeupDate, userid]);

  useEffect(() => {
    const regionParam = params.get("region");
    const furnaceParam = params.get("furnace");
    const scanRaw = params.get("scan") || "";
    const parsed = scanRaw
      ? parseScanPayload(scanRaw)
      : regionParam && furnaceParam
        ? { region: regionParam, furnaceNo: furnaceParam }
        : null;
    if (!parsed) {
      nav("/scan-sim", { replace: true });
      return;
    }
    setSuccess("");
    setAlreadyOn(false);
    // 向导式：班次 → 用工类型 → 是否半班 → 工种，均需用户依次主动选择，不预选
    setSquad(null);
    setSquadlessMode(false);
    setEmployType("");
    setHalf(false);
    setHalfOpen(false); // 进页面默认收起、整班
    setPost("");
    // 用工作点唯一 code 解析（兼容旧的 region+工作点编号），以 DB 记录为准回填工作点上下文
    getFurnaces().then((r) => {
      const item =
        (parsed.code
          ? r.items.find((f) => f.code === parsed.code)
          : r.items.find((f) => f.region === parsed.region && f.furnaceNo === parsed.furnaceNo)) ??
        null;
      if (!item) {
        Toast.show({ content: "未找到该二维码对应的工作点" });
        nav("/scan-sim", { replace: true });
        return;
      }
      setFurnaceItem(item);
      setRegion(item.region);
      setFurnaceNo(item.furnaceNo);
      setFurnaceType(item.furnaceType);
      getSelectablePosts(item.region, item.furnaceNo).then((res) => {
        setPosts(res.posts);
      });
      // 自动预选班次/用工类型/工种（仅当天扫码，不含补录；后端按「不分班优先 + 常用/上次工种且可选」给默认）
      if (!isMakeup && userid) {
        getScanSuggest(item.region, item.furnaceNo, userid)
          .then((s) => {
            if (s.squadless) setSquadlessMode(true);
            else if (s.squad) setSquad(s.squad);
            if (s.employType) setEmployType(s.employType);
            if (s.post) setPost(s.post);
          })
          .catch(() => undefined);
      }
    });
  }, [params, nav, isMakeup, userid]);

  const showSquad = useMemo(() => {
    if (!furnaceItem || !post) return false;
    const rules = furnaceItem.quotaRules.filter((r) => r.post === post);
    return rules.length > 0 && rules.every((r) => !r.squadless);
  }, [furnaceItem, post]);

  // 不分班编制（值班长/维修工等）：其 quota 规则 squad 为空。按可选工种分两类：
  // 分班工种走「① 选班次」，不分班工种走「① 不分班」轨道（不必选具体哪一班）。
  const squadlessPosts = useMemo(() => {
    if (!furnaceItem) return [] as string[];
    return posts.filter((p) => {
      const rules = furnaceItem.quotaRules.filter((r) => r.post === p);
      return rules.length > 0 && rules.every((r) => r.squadless);
    });
  }, [furnaceItem, posts]);
  const squadPosts = useMemo(
    () => posts.filter((p) => !squadlessPosts.includes(p)),
    [posts, squadlessPosts]
  );
  // 有分班工种（或工种未加载）时展示班次按钮；有不分班工种时展示「不分班」按钮
  const showSquadButtons = squadPosts.length > 0 || squadlessPosts.length === 0;
  const showSquadlessOption = squadlessPosts.length > 0;
  const step1Done = squadlessMode || !!squad;
  const visiblePosts = squadlessMode ? squadlessPosts : squadPosts;

  // 用工类型始终保留正式/实习/待岗三项可选（满编不再剔除正式）
  const allowedTypes = useMemo(() => ["正式", "实习", "待岗"], []);

  // 选中工种的正式岗剩余额度。仅剩半天(0<剩余<1)的正式岗：上岗自动按 0.5 提交（二次确认会标明）；
  // 其余默认整天 1.0。整班/半班选择器仍全程隐藏，仅在用户点「工时」展开后可手动改。
  const selectedQuota = post ? quotaByPost[post] : undefined;
  const selectedRemaining = selectedQuota
    ? selectedQuota.capacity - selectedQuota.formalUsed
    : Infinity;
  const autoHalf =
    !isMakeup &&
    employType === "正式" &&
    selectedQuota != null &&
    selectedRemaining > 0 &&
    selectedRemaining < 1;
  const effectiveHalf = half || autoHalf;

  useEffect(() => {
    if (!region || !furnaceNo || !post || !scanDate) return;
    // 分班岗未选班次时不查 quota（否则后端按 squadless 找不到规则刷 404 噪声）
    if (squadPosts.includes(post) && !squad) return;
    let cancelled = false;
    const postName = post;
    const qParams: Record<string, string> = { region, furnace: furnaceNo, post: postName };
    const squadParam = squadForPost(furnaceItem, postName, squad);
    if (squadParam) qParams.squad = squadParam;
    getQuota(qParams, scanDate)
      .then((q) => {
        if (cancelled) return;
        const types = q.allowedEmployTypes ?? ["正式", "实习", "待岗"];
        setQuotaByPost((prev) => ({
          ...prev,
          [postName]: {
            formalUsed: q.formalUsed ?? 0,
            capacity: q.capacity ?? 1,
            allowedEmployTypes: types,
          },
        }));
      })
      .catch(() => {
        /* quota refresh must not overwrite success toast */
      });
    return () => {
      cancelled = true;
    };
  }, [region, furnaceNo, post, squad, scanDate, furnaceItem]);

  useEffect(() => {
    if (!region || !furnaceNo || !posts.length || !scanDate || !furnaceItem) return;
    let cancelled = false;
    const squadVal = squad;
    Promise.all(
      posts.map(async (p) => {
        // 分班岗未选班次时跳过查询（避免 squadless 404 噪声），给中性占位
        if (squadPosts.includes(p) && !squadVal) {
          return [p, { formalUsed: 0, capacity: 1, allowedEmployTypes: ["正式", "实习", "待岗"] as string[] }] as const;
        }
        const qParams: Record<string, string> = { region, furnace: furnaceNo, post: p };
        const squadParam = squadForPost(furnaceItem, p, squadVal);
        if (squadParam) qParams.squad = squadParam;
        try {
          const q = await getQuota(qParams, scanDate);
          return [
            p,
            {
              formalUsed: q.formalUsed ?? 0,
              capacity: q.capacity ?? 1,
              allowedEmployTypes: q.allowedEmployTypes ?? ["正式", "实习", "待岗"],
            },
          ] as const;
        } catch {
          return [
            p,
            {
              formalUsed: 0,
              capacity: 1,
              allowedEmployTypes: ["正式", "实习", "待岗"] as string[],
            },
          ] as const;
        }
      })
    ).then((entries) => {
      if (cancelled) return;
      setQuotaByPost((prev) => {
        const next = { ...prev };
        for (const [p, v] of entries) next[p] = v;
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [region, furnaceNo, posts, squad, scanDate, furnaceItem]);

  useEffect(() => {
    if (!userid || !scanDate || !month) {
      setAlreadyOn(false);
      return;
    }
    let cancelled = false;
    setAlreadyOn(false);
    const uid = userid;
    const date = scanDate;
    apiGet<{ days: { date: string; status: string }[] }>(
      `/attendance?userid=${encodeURIComponent(uid)}&month=${month}`
    )
      .then((att) => {
        if (cancelled) return;
        const day = att.days.find((d) => d.date === date);
        // 补录模式下「待审」也视为已有记录，防止重复提交
        setAlreadyOn(day?.status === "正常" || (isMakeup && day?.status === "待审"));
      })
      .catch(() => {
        if (!cancelled) setAlreadyOn(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userid, scanDate, month, isMakeup]);

  const submit = async () => {
    if (!userid) {
      Toast.show({ content: "请先返回首页登录" });
      return;
    }
    if (alreadyOn) return;
    setSubmitting(true);
    if (isMakeup) {
      // 补录：提交申请单（不直接生成扫码记录，待值班长审核）
      try {
        await apiPost("/makeup", {
          userid,
          date: makeupDate,
          furnace: furnaceNo,
          region,
          squad: showSquad ? squad : null,
          post,
          employType,
          reason,
          workHours: effectiveHalf ? 4 : 8,
        });
        Toast.show({ content: "补录已提交，待值班长审核" });
        setSuccess(`补录已提交 · ${makeupDate} · ${furnaceNo} / ${post}`);
        setAlreadyOn(true);
        setTimeout(() => nav("/calendar"), 1200);
      } catch (e: unknown) {
        const err = e as { data?: { error?: string; detail?: string }; message?: string };
        const msg = err.data?.error || err.data?.detail || err.message || "提交失败";
        Toast.show({ content: typeof msg === "string" ? msg : "提交失败" });
      } finally {
        setSubmitting(false);
      }
      return;
    }
    try {
      const res = await apiPost<{
        ok?: boolean;
        warning?: { formalFull?: boolean; message?: string };
      }>("/scan", {
        userid,
        region,
        furnace: furnaceNo,
        squad: showSquad ? squad : null,
        post,
        employType,
        natualDate: scanDate,
        workHours: effectiveHalf ? 4 : 8,
      });
      if (showSquad && squad) {
        localStorage.setItem(squadStorageKey(region, furnaceNo), squad);
      }
      // 正式岗满编为软提醒：扫码已成功，仅 Toast 告知值班长按时长口径已满
      if (res?.warning?.formalFull) {
        Toast.show({ content: res.warning.message || "本班组正式岗已按出勤时长满编" });
      } else {
        Toast.show({ content: "上岗成功" });
      }
      setSuccess(`上岗成功 · ${furnaceNo} / ${post}${showSquad && squad ? ` / ${squad}` : ""}`);
      setAlreadyOn(true);
      // 上岗成功后返回首页；首页重新挂载会刷新「今日打卡」等信息
      setTimeout(() => nav("/"), 900);
      const qParams: Record<string, string> = { region, furnace: furnaceNo, post };
      const squadParam = squadForPost(furnaceItem, post, squad);
      if (squadParam) qParams.squad = squadParam;
      try {
        const q = await getQuota(qParams, scanDate);
        const types = q.allowedEmployTypes ?? ["正式", "实习", "待岗"];
        setQuotaByPost((prev) => ({
          ...prev,
          [post]: {
            formalUsed: q.formalUsed ?? 0,
            capacity: q.capacity ?? 1,
            allowedEmployTypes: types,
          },
        }));
      } catch {
        /* refresh failure must not clear success */
      }
    } catch (e: unknown) {
      const err = e as {
        data?: {
          duplicate?: boolean;
          existingRegion?: string | null;
          existingFurnace?: string | null;
          existingFurnaceType?: string | null;
          existingChief?: string | null;
          error?: string;
          detail?: string;
        };
        message?: string;
      };
      // 满编已改为后端 201+warning 软提醒，这里只剩重复上岗(409)与真错误两类
      if (err.data?.duplicate) {
        setAlreadyOn(true);
        const where =
          err.data.existingRegion && err.data.existingFurnace
            ? `${err.data.existingRegion} · ${err.data.existingFurnace}`
            : null;
        void Dialog.alert({
          title: "如需加班请联系值班长操作",
          content: (
            <div style={{ fontSize: "1.05rem", lineHeight: 2 }}>
              <div>您今天已上岗，如需加班请联系值班长操作。</div>
              <div>
                工作点编号：<strong>{where ?? "-"}</strong>
              </div>
              <div>
                设备类型：<strong>{err.data.existingFurnaceType ?? "-"}</strong>
              </div>
              <div>
                值班长：<strong>{err.data.existingChief ?? "-"}</strong>
              </div>
            </div>
          ),
          confirmText: "我知道了",
        });
      } else {
        const msg = err.data?.error || err.data?.detail || err.message || "提交失败";
        Toast.show({ content: typeof msg === "string" ? msg : "提交失败" });
      }
    } finally {
      setSubmitting(false);
    }
  };

  // 点「上岗」：若该岗位被自动按半天（仅剩半天），先弹大字提示再进二次确认
  const onSubmitClick = () => {
    if (autoHalf && !half) {
      void Dialog.confirm({
        content: (
          <div style={{ textAlign: "center", padding: "0.4rem 0" }}>
            <div
              style={{ fontSize: "1.7rem", fontWeight: 800, color: "#fa8c16", lineHeight: 1.3, marginBottom: 10 }}
            >
              该岗位只剩半个班
            </div>
            <div style={{ fontSize: "1.05rem", color: "#555" }}>
              本次上岗将按「半个班（0.5）」记录，是否继续？
            </div>
          </div>
        ),
        confirmText: "继续上岗",
        cancelText: "返回",
        onConfirm: () => setConfirmOpen(true),
      });
    } else {
      setConfirmOpen(true);
    }
  };

  if (!region || !furnaceNo) {
    return null;
  }

  // 向导：必须依次选齐 班次(或不分班)→用工类型→工种 才能确认
  const allDone = step1Done && !!employType && !!post;
  const btnDisabled = alreadyOn || submitting || !allDone;
  let btnLabel = isMakeup ? "确认补录" : "确认上岗";
  if (alreadyOn) btnLabel = isMakeup ? "该日已有记录 / 待审" : "您今天已上岗";
  else if (submitting) btnLabel = "提交中…";
  else if (!allDone) btnLabel = "请依次选择班次 / 用工类型 / 工种";

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh" }}>
      <NavBar
        onBack={() => nav(isMakeup ? "/calendar" : "/")}
        style={{ background: isMakeup ? "#fa8c16" : "#1677FF", color: "#fff" }}
      >
        {isMakeup ? "漏扫补录" : "扫码上岗"}
      </NavBar>
      {isMakeup && (
        <div
          data-testid="makeup-banner"
          style={{
            margin: "12px 12px 0",
            padding: "10px 12px",
            background: "#fff7e6",
            border: "1px solid #ffd591",
            borderRadius: 8,
            fontSize: 13,
            color: "#ad6800",
          }}
        >
          正在补录 <strong>{makeupDate}</strong> 的上岗信息（非当天），提交后需值班长审核。
        </div>
      )}
      <div style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div style={{ color: "#1677FF", fontWeight: 600 }}>工作点信息卡</div>
            <div data-testid="scan-furnace-label" style={{ fontSize: 16, fontWeight: 600, marginTop: 2 }}>
              {furnaceItem ? `${region} · ${furnaceNo}` : <DotLoading color="primary" />}
            </div>
            {furnaceType && <div style={{ color: "#888", fontSize: 12 }}>型号：{furnaceType}</div>}
            <div style={{ fontSize: 12, marginTop: 2, color: furnaceItem?.chiefName ? "#1677FF" : "#bbb" }}>
              {furnaceItem?.chiefName ? `值班长：${furnaceItem.chiefName}` : "未绑定值班长"}
            </div>
          </div>
        </div>
        <div
          data-testid="scan-employee-info"
          style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid #f0f0f0" }}
        >
          <EmployeeBadge name={name} userid={userid} />
        </div>
      </div>
      {/* ① 班次（切换清空下级）；不分班编制走「不分班」轨道，不必选具体哪一班 */}
      <div data-testid="squad-select" style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8 }}>
        <div style={{ fontWeight: 600, marginBottom: 8, fontSize: "1.15rem" }}>① 选择班次</div>
        {showSquadButtons &&
          SQUADS.map((s) => (
            <button
              key={s}
              type="button"
              data-testid={`squad-${s}`}
              onClick={() => {
                setSquad(s);
                setSquadlessMode(false);
                setEmployType("");
                setHalf(false);
                setPost("");
              }}
              style={{
                marginRight: 8,
                marginBottom: 8,
                padding: "0.7rem 1.3rem",
                fontSize: "1.1rem",
                border: !squadlessMode && squad === s ? "2px solid #1677FF" : "1px solid #ddd",
                borderRadius: 8,
                background: !squadlessMode && squad === s ? "#f0f7ff" : "#fff",
              }}
            >
              {s}
            </button>
          ))}
        {showSquadlessOption && (
          <button
            type="button"
            data-testid="squad-none"
            onClick={() => {
              setSquadlessMode(true);
              setSquad(null);
              setEmployType("");
              setHalf(false);
              setPost("");
            }}
            style={{
              marginRight: 8,
              marginBottom: 8,
              padding: "0.7rem 1.3rem",
              fontSize: "1.1rem",
              border: squadlessMode ? "2px solid #1677FF" : "1px solid #ddd",
              borderRadius: 8,
              background: squadlessMode ? "#f0f7ff" : "#fff",
            }}
          >
            不分班
          </button>
        )}
      </div>

      {/* ② 用工类型（需先选班次，切换清空下级）；半个班开关默认收起在标题右侧（所有用工类型可选，默认整班） */}
      <div
        data-testid="employ-type"
        style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8, opacity: step1Done ? 1 : 0.45 }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 8,
          }}
        >
          <span style={{ fontWeight: 600, fontSize: "1.15rem" }}>
            ② 选择用工类型{!step1Done && <span style={{ fontSize: 12, color: "#fa8c16", fontWeight: 400 }}>（请先选择班次）</span>}
          </span>
          {/* 工时：所有用工类型均可选。默认收起，右侧只一个带 icon 的小按钮；点击展开后
              整班/半个班 选择渲染在「选择用工类型」下方（不占标题高度），展开后不可收起；
              进页面重置为收起+整班。 */}
          {!!employType && !halfOpen && (
            <button
              type="button"
              data-testid="half-expand"
              onClick={() => setHalfOpen(true)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                padding: "0.3rem 0.7rem",
                fontSize: 13,
                color: "#999",
                background: "#fafafa",
                border: "1px solid #eee",
                borderRadius: 16,
              }}
            >
              <span style={{ fontSize: 14 }}>◐</span>工时
            </button>
          )}
        </div>
        {step1Done && (
          <Selector
            options={allowedTypes.map((t) => ({ label: t, value: t }))}
            value={employType ? [employType] : []}
            onChange={(v) => {
              if (!v[0]) return;
              setEmployType(v[0] as string);
              setHalf(false);
              setPost("");
            }}
            style={{ "--border-radius": "8px" } as React.CSSProperties}
          />
        )}
        {/* 展开后的 整班/半个班：放在用工类型下方 */}
        {!!employType && halfOpen && (
          <div data-testid="half-select" style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {[
              { label: "整班", v: false },
              { label: "半个班", v: true },
            ].map((o) => {
              const active = half === o.v;
              return (
                <button
                  key={o.label}
                  type="button"
                  data-testid={`half-${o.v ? "yes" : "no"}`}
                  onClick={() => setHalf(o.v)}
                  style={{
                    padding: "0.7rem 1.3rem",
                    fontSize: "1.1rem",
                    border: active ? "2px solid #1677FF" : "1px solid #ddd",
                    borderRadius: 8,
                    background: active ? "#f0f7ff" : "#fff",
                    color: "#333",
                  }}
                >
                  {o.label}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* ③ 工种（需先选用工类型；正式按剩余编制门控，半班拆分两半位） */}
      <div
        data-testid="post-select"
        style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8, opacity: employType ? 1 : 0.45 }}
      >
        <div style={{ fontWeight: 600, marginBottom: 8, fontSize: "1.15rem" }}>
          ③ 选择工种{!employType && <span style={{ fontSize: 12, color: "#fa8c16", fontWeight: 400 }}>（请先选择用工类型）</span>}
        </div>
        {employType &&
          visiblePosts.map((p) => {
            const q = quotaByPost[p];
            const remaining = q ? q.capacity - q.formalUsed : 0;
            const isFormal = employType === "正式";
            const remainDisplay = Math.max(0, remaining);
            // 正式岗：剩余>0 即可选；剩余<=0（满编）置灰不可选。0<剩余<1（仅剩半天）时，
            // 上岗将自动按半天(0.5)提交（二次确认弹框会标明）。实习/待岗不占正式编制，始终可选。
            const noSpace = isFormal && q != null && remaining <= 0;
            const willHalf = isFormal && q != null && remaining > 0 && remaining < 1;
            const selected = post === p;
            const label = q
              ? isFormal
                ? `正式岗 剩余 ${remainDisplay} / 编制 ${q.capacity}`
                : `编制 ${q.capacity}（${employType}不占正式岗）`
              : "…";
            const hint = noSpace ? " · 已满编" : willHalf ? " · 上岗将记半天(0.5)" : "";
            const tagColor = noSpace ? "#bbb" : willHalf ? "#fa8c16" : "#1677FF";

            return (
              <button
                key={p}
                type="button"
                data-testid={`post-${p}`}
                disabled={noSpace}
                onClick={() => setPost(p)}
                style={{
                  display: "block",
                  width: "100%",
                  marginBottom: 8,
                  padding: "0.9rem 1rem",
                  fontSize: "1.1rem",
                  border: selected ? "2px solid #1677FF" : "1px solid #ddd",
                  borderRadius: 8,
                  background: noSpace ? "#f5f5f5" : selected ? "#f0f7ff" : "#fff",
                  color: noSpace ? "#bbb" : "#333",
                }}
              >
                {p}
                <div data-testid={`quota-${p}`} style={{ fontSize: 12, color: tagColor }}>
                  {label}
                  {hint}
                </div>
              </button>
            );
          })}
      </div>

      {/* 补录原因（仅补录模式） */}
      {isMakeup && (
        <div style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8 }}>
          <div style={{ fontWeight: 600, marginBottom: 8, fontSize: "1.15rem" }}>补录原因</div>
          <input
            data-testid="makeup-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="如:漏扫 / 设备故障"
            style={{
              width: "100%",
              padding: 10,
              border: "1px solid #ddd",
              borderRadius: 8,
              fontSize: 14,
              boxSizing: "border-box",
            }}
          />
        </div>
      )}

      <button
        type="button"
        data-testid="submit-scan"
        onClick={onSubmitClick}
        disabled={btnDisabled}
        style={{
          margin: 12,
          width: "calc(100% - 24px)",
          padding: 12,
          background: btnDisabled ? "#ccc" : "#1677FF",
          color: "#fff",
          border: "none",
          borderRadius: 8,
        }}
      >
        {btnLabel}
      </button>
      {success && (
        <p data-testid="scan-success" style={{ margin: 12, color: "#1677FF" }}>
          {success}
        </p>
      )}

      {/* 二次确认：核对全部信息（适老化：大字号、大行距、大按钮） */}
      <Popup visible={confirmOpen} onMaskClick={() => setConfirmOpen(false)} bodyStyle={{ padding: "1.4rem 1.2rem" }}>
        <div style={{ fontWeight: 700, fontSize: "1.45rem", marginBottom: "1rem", textAlign: "center" }}>
          {isMakeup ? "请核对补录信息" : "请核对上岗信息"}
        </div>
        <div data-testid="confirm-summary" style={{ fontSize: "1.2rem", lineHeight: 2.2 }}>
          {[
            ...(isMakeup ? [["补录日期", makeupDate]] : []),
            ["姓名", name || "-"],
            ["工号", userid || "-"],
            ["地区", region],
            ["工作点", furnaceNo],
            ["型号", furnaceType || "-"],
            ["值班长", furnaceItem?.chiefName || "未绑定"],
            ["班次", showSquad ? squad || "-" : "不分班"],
            ["工种", post || "-"],
            ["用工类型", employType || "-"],
            ["出勤", effectiveHalf ? (autoHalf && !half ? "半个班（该岗仅剩半天）" : "半个班") : "整班"],
            ...(isMakeup ? [["原因", reason || "-"]] : []),
          ].map(([k, v]) => (
            <div
              key={k}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 16,
                padding: "0.15rem 0",
                borderBottom: "1px solid #f5f5f5",
              }}
            >
              <span style={{ color: "#888", flexShrink: 0 }}>{k}</span>
              <span style={{ fontWeight: 600, textAlign: "right" }}>{v}</span>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", gap: 12, marginTop: "1.4rem" }}>
          <Button
            size="large"
            style={{ flex: 1, fontSize: "1.2rem", "--border-radius": "10px" } as React.CSSProperties}
            fill="outline"
            onClick={() => setConfirmOpen(false)}
          >
            返回修改
          </Button>
          <Button
            size="large"
            style={{ flex: 1, fontSize: "1.2rem", "--border-radius": "10px" } as React.CSSProperties}
            color="primary"
            loading={submitting}
            data-testid="confirm-submit-scan"
            onClick={async () => {
              await submit();
              setConfirmOpen(false);
            }}
          >
            {isMakeup ? "确认补录" : "确认上岗"}
          </Button>
        </div>
      </Popup>
    </div>
  );
}
