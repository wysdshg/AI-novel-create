<template>
  <div class="nf-page">
    <div class="nf-box">
      <div class="nf-code">404</div>
      <h2 class="nf-title">这个页面不存在</h2>
      <p class="nf-sub">
        可能是链接过期、地址打错，或者这个功能已经下线了。
      </p>
      <p class="nf-path">
        当前地址：<code>{{ path }}</code>
      </p>
      <div class="nf-actions">
        <el-button type="primary" @click="go('/workspace/chat')">回到对话</el-button>
        <el-button @click="go('/workspace/overview')">看概览</el-button>
        <el-button text @click="back">返回上一页</el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const path = computed(() => route.fullPath)

function go(to) {
  router.push(to)
}
function back() {
  // 直接输入地址进来时没有历史记录 → 回退会退出站点，故兜底回对话
  if (window.history.length > 1) router.back()
  else router.push('/workspace/chat')
}
</script>

<style scoped>
.nf-page {
  min-height: 60vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
.nf-box {
  text-align: center;
  max-width: 460px;
}
.nf-code {
  font-size: 72px;
  font-weight: 600;
  line-height: 1;
  color: var(--el-border-color);
  letter-spacing: 2px;
}
.nf-title {
  font-size: 18px;
  font-weight: 600;
  margin: 16px 0 8px;
}
.nf-sub {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin: 0 0 12px;
  line-height: 1.7;
}
.nf-path {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 0 0 20px;
  word-break: break-all;
}
.nf-path code {
  background: var(--el-fill-color-light);
  padding: 2px 6px;
  border-radius: 4px;
}
.nf-actions {
  display: flex;
  gap: 8px;
  justify-content: center;
  flex-wrap: wrap;
}
</style>
