<template>
  <div class="app-layout">
    <aside class="sidebar">
      <div class="logo">
        <h2>巡检工单</h2>
        <span class="subtitle">流程自动化</span>
      </div>
      <nav class="nav-menu">
        <router-link to="/dashboard" class="nav-item" active-class="active">
          <span class="nav-icon">📊</span>
          <span>数据概览</span>
        </router-link>
        <router-link to="/work-orders" class="nav-item" active-class="active">
          <span class="nav-icon">📋</span>
          <span>工单列表</span>
        </router-link>
        <router-link to="/monitor" class="nav-item" active-class="active">
          <span class="nav-icon">📡</span>
          <span>监控触发</span>
        </router-link>
        <router-link to="/sync" class="nav-item" active-class="active">
          <span class="nav-icon">🔄</span>
          <span>数据同步</span>
        </router-link>
        <router-link to="/email-tool" class="nav-item" active-class="active">
          <span class="nav-icon">📧</span>
          <span>邮件发送</span>
        </router-link>
      </nav>
      <div class="sidebar-footer">
        <div :class="['health-dot', healthOk ? 'ok' : 'err']"></div>
        <span class="health-text">{{ healthOk ? '服务正常' : '服务异常' }}</span>
      </div>
    </aside>
    <main class="main-content">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getHealth, createEventStream } from './api'

const healthOk = ref(true)

onMounted(async () => {
  try {
    const h = await getHealth()
    healthOk.value = h.status === 'healthy'
  } catch {
    healthOk.value = false
  }

  // Connect WebSocket for real-time events
  createEventStream((_type, _data) => {
    // Events can be used to trigger toast notifications or data refresh
    // Individual pages handle their own refresh logic
  })
})
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: #f0f2f5;
  color: #333;
}

.app-layout {
  display: flex;
  min-height: 100vh;
}

.sidebar {
  width: 220px;
  background: #1d1e2c;
  color: #fff;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.logo {
  padding: 24px 20px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.logo h2 {
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 2px;
}

.logo .subtitle {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
}

.nav-menu {
  flex: 1;
  padding: 12px 0;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  color: rgba(255, 255, 255, 0.65);
  text-decoration: none;
  font-size: 14px;
  transition: all 0.2s;
}

.nav-item:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.06);
}

.nav-item.active {
  color: #fff;
  background: rgba(64, 158, 255, 0.15);
  border-right: 3px solid #409eff;
}

.nav-icon {
  font-size: 16px;
  width: 20px;
  text-align: center;
}

.sidebar-footer {
  padding: 16px 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  display: flex;
  align-items: center;
  gap: 8px;
}

.health-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.health-dot.ok {
  background: #52c41a;
  box-shadow: 0 0 6px rgba(82, 196, 26, 0.4);
}

.health-dot.err {
  background: #ff4d4f;
  box-shadow: 0 0 6px rgba(255, 77, 79, 0.4);
}

.health-text {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}

.main-content {
  flex: 1;
  padding: 24px;
  overflow-y: auto;
  max-height: 100vh;
}

/* Page header style */
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}

.page-header h1 {
  font-size: 22px;
  font-weight: 600;
  color: #1a1a2e;
}

/* Card style */
.card {
  background: #fff;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}

/* Stat cards row */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.stat-card {
  background: #fff;
  border-radius: 8px;
  padding: 18px 20px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}

.stat-card .label {
  font-size: 13px;
  color: #8c8c8c;
  margin-bottom: 6px;
}

.stat-card .value {
  font-size: 28px;
  font-weight: 700;
  color: #1a1a2e;
}

.stat-card .value.success { color: #52c41a; }
.stat-card .value.warning { color: #faad14; }
.stat-card .value.danger { color: #ff4d4f; }
.stat-card .value.info { color: #409eff; }

/* Charts grid */
.charts-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
  margin-bottom: 20px;
}

.chart-card {
  background: #fff;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}

.chart-card h3 {
  font-size: 15px;
  font-weight: 600;
  color: #333;
  margin-bottom: 12px;
}

.chart-container {
  height: 280px;
}

/* Full-width chart */
.chart-full {
  grid-column: 1 / -1;
}
</style>
