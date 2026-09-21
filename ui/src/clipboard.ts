import { execFile } from "node:child_process";

function run(command: string, args: string[], input?: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = execFile(command, args, { encoding: "utf8", windowsHide: true }, (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    });
    if (input !== undefined && child.stdin) child.stdin.end(input);
  });
}

/** 从系统剪贴板读取文本。Windows 是本项目的首要运行平台。 */
export async function readClipboard(): Promise<string> {
  if (process.platform === "win32") {
    return run("powershell.exe", ["-NoProfile", "-Command", "Get-Clipboard -Raw"]);
  }
  if (process.platform === "darwin") return run("pbpaste", []);
  return run("xclip", ["-selection", "clipboard", "-o"]);
}

/** 把文本交给系统剪贴板，而不是依赖用户终端的选择复制快捷键。 */
export async function writeClipboard(text: string): Promise<void> {
  if (process.platform === "win32") {
    await run("clip.exe", [], text);
    return;
  }
  if (process.platform === "darwin") {
    await run("pbcopy", [], text);
    return;
  }
  await run("xclip", ["-selection", "clipboard"], text);
}
