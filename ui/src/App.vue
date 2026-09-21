<script setup lang="ts">
import { computed, onScopeDispose, ref } from "vue";
import { Box, Text, useApp, useInput } from "@vue-tui/runtime";
import { BackendClient } from "./backend";
import { readClipboard, writeClipboard } from "./clipboard";

type Page = "sniff" | "scan" | "convert" | "check";
type Result = { summary?: Record<string, unknown>; items?: Record<string, unknown>[]; plan?: Record<string, unknown>[]; results?: Record<string, unknown>[]; needs_confirmation?: boolean };

const backend = new BackendClient();
const { exit } = useApp();
const page = ref<Page>("sniff");
const path = ref(".");
const target = ref("utf-8");
const eol = ref("keep");
const focus = ref(0);
const editing = ref(false);
const result = ref<Result | null>(null);
const message = ref("按 1–4 切换功能，Tab 编辑字段，Enter 执行。");
const waitingConfirmation = ref(false);
const busy = ref(false);

const fields = computed(() => page.value === "convert" ? [path, target, eol]
  : page.value === "check" ? [path, target, eol] : [path]);
const fieldLabels = computed(() => page.value === "convert" ? ["路径", "目标编码", "换行"]
  : page.value === "check" ? ["路径", "期望编码", "换行"] : ["路径"]);

function setPage(next: Page): void {
  page.value = next;
  focus.value = 0;
  editing.value = false;
  result.value = null;
  waitingConfirmation.value = false;
  message.value = `已切换到${{ sniff: "嗅探", scan: "扫描", convert: "转码", check: "检查" }[next]}。`;
}

function leaveEditing(): void {
  editing.value = false;
  message.value = "已返回功能选择；可按 1–4 或 F1–F4 切换功能。";
}

function lines(): string[] {
  if (!result.value) return [];
  if (result.value.plan) return result.value.plan.slice(0, 18).map(item =>
    `${item.status === "convert" ? "→" : item.status === "error" ? "!" : "·"} ${item.path}  ${item.reason || ""}`);
  const entries = result.value.results || result.value.items || [];
  return entries.slice(0, 18).map(item => {
    const problems = Array.isArray(item.problems) && item.problems.length ? `  ! ${item.problems.join("；")}` : "";
    return `${item.path ?? ""}  ${item.label ?? item.encoding ?? ""}  ${item.confidence ?? ""}${problems}`;
  });
}

function appendToField(text: string): void {
  // 单行字段不接受回车；Windows 剪贴板与括号粘贴常在尾部携带 CRLF。
  fields.value[focus.value].value += text.replace(/[\r\n]+/g, "");
}

function copyContent(): string {
  const form = fieldLabels.value.map((label, index) => `${label}：${fields.value[index].value}`);
  return [
    "OpenBatchTranscoding · Vue TUI",
    `功能：${page.value}`,
    ...form,
    "",
    message.value,
    ...lines(),
  ].join("\n");
}

async function pasteFromClipboard(): Promise<void> {
  try {
    editing.value = true;
    appendToField(await readClipboard());
    message.value = "已从系统剪贴板粘贴到当前字段。";
  } catch (error) {
    message.value = `无法读取剪贴板：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function copyToClipboard(): Promise<void> {
  try {
    await writeClipboard(copyContent());
    message.value = "当前页面内容已复制到系统剪贴板。";
  } catch (error) {
    message.value = `无法写入剪贴板：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function run(confirm = false): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    const params: Record<string, unknown> = { paths: path.value };
    let action = page.value;
    if (page.value === "convert") Object.assign(params, { to: target.value, eol: eol.value, confirm });
    if (page.value === "check") Object.assign(params, { expect: target.value, eol: eol.value });
    result.value = await backend.request(action, params) as Result;
    waitingConfirmation.value = page.value === "convert" && Boolean(result.value.needs_confirmation);
    message.value = waitingConfirmation.value
      ? "计划已生成。按 Y 确认写盘，按 Esc 取消。"
      : `完成：${JSON.stringify(result.value.summary || {})}`;
  } catch (error) {
    message.value = `失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    busy.value = false;
  }
}

useInput((event) => {
  if (event.type === "text" || event.type === "paste") {
    if (waitingConfirmation.value) {
      if (event.text.toLowerCase() === "y") void run(true);
      return;
    }
    // 未编辑字段时，数字是功能快捷键；进入编辑状态后才作为字段内容。
    if (!editing.value && event.type === "text") {
      if (event.text === "1") setPage("sniff");
      if (event.text === "2") setPage("scan");
      if (event.text === "3") setPage("convert");
      if (event.text === "4") setPage("check");
      if (event.text.toLowerCase() === "q") exit();
      return;
    }
    editing.value = true;
    appendToField(event.text);
    return;
  }
  if (event.type !== "key") return;
  const key = event.key;
  // F 键是全局快捷键：即使光标正在编辑路径，也可以直接切换页面。
  if (key.name === "f1") setPage("sniff");
  if (key.name === "f2") setPage("scan");
  if (key.name === "f3") setPage("convert");
  if (key.name === "f4") setPage("check");
  if (!editing.value) {
    if (key.character === "1") setPage("sniff");
    if (key.character === "2") setPage("scan");
    if (key.character === "3") setPage("convert");
    if (key.character === "4") setPage("check");
    if (key.character === "q") exit();
  }
  if (key.ctrl && key.character === "c") {
    void copyToClipboard();
    return;
  }
  if (key.ctrl && key.character === "v" && !waitingConfirmation.value) {
    void pasteFromClipboard();
    return;
  }
  if (key.name === "tab") {
    if (key.shift && editing.value) leaveEditing();
    else if (!editing.value) editing.value = true;
    else focus.value = (focus.value + 1) % fields.value.length;
  }
  if (key.name === "backspace" && !waitingConfirmation.value) {
    const field = fields.value[focus.value];
    field.value = field.value.slice(0, -1);
  }
  if (key.name === "enter" && !waitingConfirmation.value) {
    editing.value = false;
    void run();
  }
  if (key.name === "escape") {
    if (waitingConfirmation.value) {
      waitingConfirmation.value = false;
      message.value = "已取消写盘；计划仍可查看。";
    } else if (editing.value) {
      leaveEditing();
    }
  }
});

onScopeDispose(() => backend.close());
</script>

<template>
  <Box flexDirection="column" :padding="1" :gap="1">
    <Box borderStyle="round" borderColor="green" :paddingX="1" flexDirection="column">
      <Text bold color="green">OpenBatchTranscoding · Vue TUI</Text>
      <Text dimColor>1 嗅探  2 扫描  3 转码  4 检查  F1–F4 随时切换  Tab 编辑/切换字段  Shift+Tab/Esc 返回功能  Enter 执行</Text>
      <Text dimColor>Ctrl+V 粘贴  Ctrl+C 复制  q 退出</Text>
    </Box>

    <Box flexDirection="column" borderStyle="single" :paddingX="1">
      <Text v-for="(label, index) in fieldLabels" :key="label" :color="editing && focus === index ? 'cyan' : 'default'">
        {{ editing && focus === index ? '›' : ' ' }} {{ label }}：{{ fields[index].value }}
      </Text>
    </Box>

    <Box flexDirection="column" borderStyle="single" :paddingX="1" :flexGrow="1">
      <Text bold>{{ page.toUpperCase() }} {{ busy ? '（处理中…）' : '' }}</Text>
      <Text color="yellow">{{ message }}</Text>
      <Text v-for="line in lines()" :key="line" wrap="truncate">{{ line }}</Text>
    </Box>
  </Box>
</template>
