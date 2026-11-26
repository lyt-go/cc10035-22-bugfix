import { createRouter, createWebHistory } from 'vue-router'

import ExportDetailView from '@/views/ExportDetailView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/batch_export/:ref', name: 'export-detail', component: ExportDetailView },
    { path: '/batch_export/detail/:ref', redirect: (to) => `/batch_export/${String(to.params.ref)}` },
    { path: '/batch-export/:ref', component: ExportDetailView },
    { path: '/exports/:ref', component: ExportDetailView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
