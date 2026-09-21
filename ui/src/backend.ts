import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

type Response = { id: number; ok: boolean; result?: unknown; error?: string };

/** Python 进程只处理 JSONL，Node 进程才拥有用户终端。 */
export class BackendClient {
  private readonly process: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private readonly pending = new Map<number, {
    resolve: (value: unknown) => void;
    reject: (reason: Error) => void;
  }>();

  constructor() {
    const python = process.env.OBT_PYTHON || "python";
    const pythonPath = process.env.PYTHONPATH || "../src";
    this.process = spawn(python, ["-m", "obt.tui_backend"], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONPATH: pythonPath },
    });
    createInterface({ input: this.process.stdout }).on("line", (line) => this.receive(line));
    this.process.stderr.on("data", (data) => {
      // stderr 不进入终端画布；仅在调试环境保留给 Node 的诊断输出。
      if (process.env.OBT_TUI_DEBUG) process.stderr.write(data);
    });
    this.process.once("exit", (code) => {
      const error = new Error(`Python 后端已退出（${code ?? "未知"}）`);
      for (const pending of this.pending.values()) pending.reject(error);
      this.pending.clear();
    });
  }

  request(action: string, params: Record<string, unknown>): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process.stdin.write(`${JSON.stringify({ id, action, params })}\n`);
    });
  }

  close(): void {
    this.process.stdin.end();
    this.process.kill();
  }

  private receive(line: string): void {
    let response: Response;
    try {
      response = JSON.parse(line) as Response;
    } catch {
      return;
    }
    const pending = this.pending.get(response.id);
    if (!pending) return;
    this.pending.delete(response.id);
    if (response.ok) pending.resolve(response.result);
    else pending.reject(new Error(response.error || "后端请求失败"));
  }
}
