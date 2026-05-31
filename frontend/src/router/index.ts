import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/dashboard',
    },
    {
      path: '/dashboard',
      name: 'Dashboard',
      component: () => import('../views/Dashboard.vue'),
    },
    {
      path: '/work-orders',
      name: 'WorkOrders',
      component: () => import('../views/WorkOrders.vue'),
    },
    {
      path: '/monitor',
      name: 'Monitor',
      component: () => import('../views/Monitor.vue'),
    },
    {
      path: '/sync',
      name: 'Sync',
      component: () => import('../views/Sync.vue'),
    },
    {
      path: '/email-tool',
      name: 'EmailTool',
      component: () => import('../views/EmailTool.vue'),
    },
  ],
})

export default router
