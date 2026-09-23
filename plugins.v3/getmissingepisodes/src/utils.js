/** 剧集管家联邦界面使用的公共工具。 */

/** 默认海报占位图，与后端 default_poster_path 保持一致。 */
export const DEFAULT_POSTER = '/assets/no-image-CweBJ8Ee.jpeg'

/** 统一提取宿主 API 客户端与标准响应模型中的业务数据。 */
export function unwrapResponse(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'success')) {
    if (response.success === false) throw new Error(response.message || '操作失败')
    return response.data
  }
  return response?.data ?? response
}

/** 九类视图过滤定义，顺序与标题对应工作台统计卡片，key 与后端 tags 一致。 */
export const filterTabs = [
  { key: 'all', title: '所有剧集', icon: 'mdi-database-outline', color: 'primary' },
  { key: 'total', title: '总处理', icon: 'mdi-playlist-check', color: 'primary' },
  { key: 'all_exist', title: '全部存在', icon: 'mdi-check-all', color: 'success' },
  { key: 'finished', title: '已完结', icon: 'mdi-check-circle-outline', color: 'success' },
  { key: 'skipped', title: '已跳过', icon: 'mdi-skip-next-outline', color: 'secondary' },
  { key: 'no_exist', title: '存在缺失', icon: 'mdi-alert-outline', color: 'warning' },
  { key: 'not_all_no_exist', title: '已有季缺失', icon: 'mdi-target', color: 'error' },
  { key: 'added_rss', title: '已订阅', icon: 'mdi-rss', color: 'info' },
  { key: 'failed', title: '未识别', icon: 'mdi-bug-outline', color: 'error' },
]

/** 返回检测状态对应的主题色与图标。 */
export function existStatusMeta(status) {
  const states = {
    全部存在: { color: 'success', icon: 'mdi-check-circle-outline' },
    已加订阅: { color: 'info', icon: 'mdi-rss' },
    存在缺失: { color: 'warning', icon: 'mdi-alert-outline' },
    获取失败: { color: 'error', icon: 'mdi-help-circle-outline' },
    未知状态: { color: 'secondary', icon: 'mdi-help-box-outline' },
  }
  return states[status] || states.未知状态
}

/** 汇总一季缺失集数的展示文本。 */
export function seasonLabel(season) {
  const number = Number(season.season ?? 0)
  return `S${String(number).padStart(2, '0')}`
}

/** 把季与集缺失明细整理成紧凑的展示片段。 */
export function seasonChips(seasons = []) {
  return (seasons || []).map(season => {
    const missing = (season.episode_no_exist || []).filter(Boolean)
    return {
      season: seasonLabel(season),
      missing,
      total: season.episode_total_unfiltered || season.episode_total || 0,
      ignored: Boolean(season.ignored),
      text: missing.length
        ? `${seasonLabel(season)} 缺 ${missing.length} 集`
        : `${seasonLabel(season)} 缺整季`,
    }
  })
}

/** 统计一部剧的缺失季数与缺失集数。 */
export function countMissing(seasons = []) {
  let seasonsCount = 0
  let episodesCount = 0
  ;(seasons || []).forEach(season => {
    const missing = season.episode_no_exist || []
    if (missing.length) {
      seasonsCount += 1
      episodesCount += missing.length
    } else {
      seasonsCount += 1
      episodesCount += season.episode_total || season.episode_total_unfiltered || 0
    }
  })
  return { seasonsCount, episodesCount }
}

/** 格式化后端返回的时间文本，保留原始值兜底。 */
export function formatDateTime(value) {
  if (!value) return '暂无'
  const date = new Date(String(value).replace(' ', 'T'))
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

/** 返回缺失处理方式的中文说明。 */
export function actionHint(noExistAction) {
  const hints = {
    仅检查记录: '仅记录缺失，不自动订阅',
    添加到订阅: '发现缺失自动加入订阅',
    标记为存在: '发现缺失自动标记为存在',
  }
  return hints[noExistAction] || noExistAction || ''
}
