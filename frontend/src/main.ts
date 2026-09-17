import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIcons from '@element-plus/icons-vue'
import type { Component } from 'vue'

// 样式顺序很重要：Element Plus 在前，自有令牌与覆盖在后
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import 'highlight.js/styles/atom-one-dark.css'
import '@/styles/index.css'

import App from './App.vue'
import router from './router'
import { registerPermissionDirective } from './directives/permission'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

for (const [name, component] of Object.entries(ElementPlusIcons)) {
  app.component(name, component as Component)
}

registerPermissionDirective(app)

app.mount('#app')
