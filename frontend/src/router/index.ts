import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SamplingCredentialView from '@/views/SamplingCredentialView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    {
      path: '/downsample/credential/:taskId?',
      name: 'sampling-credential',
      component: SamplingCredentialView,
      props: true,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
