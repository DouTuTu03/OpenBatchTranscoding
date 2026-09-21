import { createApp } from "@vue-tui/runtime";
import App from "./App.vue";

// Ctrl+C 由应用用于复制结果；退出使用 q，避免抢走系统复制快捷键。
createApp(App).mount({ exitOnCtrlC: false });
