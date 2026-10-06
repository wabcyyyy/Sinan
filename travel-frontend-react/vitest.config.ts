import { defineConfig } from 'vitest/config'

// React 壳单测配置。旧组件树退役后不再需要其插件与 EP 解析器；
// happy-dom 供涉及 sessionStorage 的用例使用。
export default defineConfig({
  esbuild: { jsx: 'automatic', jsxImportSource: 'react' },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.test.ts'],
  },
})
