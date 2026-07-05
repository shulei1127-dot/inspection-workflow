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
    // /sync 路由已隐藏，数据同步日志在任务日志页面可查看
    {
      path: '/email-tool',
      name: 'EmailTool',
      component: () => import('../views/EmailTool.vue'),
    },
    {
      path: '/audit',
      name: 'Audit',
      component: () => import('../views/Audit.vue'),
    },
    {
      path: '/change-logs',
      name: 'ChangeLog',
      component: () => import('../views/ChangeLog.vue'),
    },
    {
      path: '/task-logs',
      name: 'TaskLogs',
      component: () => import('../views/TaskLogs.vue'),
    },
    {
      path: '/visit',
      name: 'Visit',
      component: () => import('../views/Visit.vue'),
    },
  ],
})

export default router
