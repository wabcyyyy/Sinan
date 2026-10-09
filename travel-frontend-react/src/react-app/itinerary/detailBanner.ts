/** 详情页生成中横幅的文案选择（M5b 遗留收口：core_ready 刷新投影）。
 *
 * 后端 core_ready 里程碑起，详情 VO 带 `coreReady=true`（M6 只读投影，按
 * gen_state 现算，刷新/轮询重建都拿得到）；详情页此前只渲染通用「正在逐日
 * 编排」——刷新后「已可查看」提示不亮。这里把横幅文案按 coreReady 二分：
 * 主行程已可查看时如实说（措辞与 home/TripPanel 的轻提示条一致），避免用户
 * 在富化窗口误以为什么都还没生成。
 */
export function detailGeneratingCopy(coreReady?: boolean): string {
  return coreReady
    ? '主行程已可查看，备选仍在完善；完成后自动更新，无需刷新页面。'
    : '司南正在逐日编排这趟旅程，完成后自动更新，无需刷新页面。'
}
