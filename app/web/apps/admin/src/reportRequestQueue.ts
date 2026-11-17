export type QueueStatus = { pending: number; label: string; waiting: boolean };
export const isReportCancelled = (error: unknown) => error instanceof Error && error.name === "AbortError";
const cancelled = () => Object.assign(new Error("请求范围已改变，已取消等待"), { name: "AbortError" });

/** 同一导出页所有重请求共用队列；已发出的请求结束后才释放位置。 */
export class ReportRequestQueue {
  private tail: Promise<unknown> = Promise.resolve();
  private requests = new Map<string, Promise<unknown>>();
  private listeners = new Set<(value: QueueStatus) => void>();
  private status: QueueStatus = { pending: 0, label: "", waiting: false };
  subscribe(listener: (value: QueueStatus) => void) {
    this.listeners.add(listener);
    listener(this.status);
    return () => { this.listeners.delete(listener); };
  }
  private publish(patch: Partial<QueueStatus>) {
    this.status = { ...this.status, ...patch, pending: this.requests.size };
    this.listeners.forEach(listener => listener(this.status));
  }
  run<T>({ key, label, current, task, maxWaitMs = 120000 }: {
    key: string; label: string; current: () => boolean; task: () => Promise<T>; maxWaitMs?: number;
  }): Promise<T> {
    const existing = this.requests.get(key);
    if (existing) return existing as Promise<T>;
    const execute = async () => {
      const deadline = Date.now() + maxWaitMs;
      this.publish({ label, waiting: false });
      while (true) {
        if (!current()) throw cancelled();
        try {
          const result = await task();
          if (!current()) throw cancelled();
          return result;
        } catch (error) {
          if (!current()) throw cancelled();
          const busy = error as { status?: number; retryAfter?: number };
          if (busy.status !== 429) throw error;
          if (Date.now() >= deadline) {
            throw Object.assign(new Error("等待计算超时，请查看占用状态后重试"), { status: 429 });
          }
          this.publish({ waiting: true });
          const seconds = Number.isFinite(busy.retryAfter) ? Math.max(0.1, busy.retryAfter!) : 2;
          const until = Math.min(deadline, Date.now() + seconds * 1000);
          // 小步等待，以便切换月份/工作点或离开页面后及时取消旧请求。
          while (Date.now() < until) {
            if (!current()) throw cancelled();
            await new Promise(resolve => setTimeout(resolve, Math.min(200, until - Date.now())));
          }
        }
      }
    };
    const promise = this.tail.then(execute).finally(() => {
      this.requests.delete(key);
      this.publish({ label: "", waiting: false });
    });
    this.requests.set(key, promise);
    this.tail = promise.catch(() => undefined);
    this.publish({});
    return promise;
  }
}
