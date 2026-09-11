/* 应用内对话框：一处挂载（App.vue 里的 <Dialog />），到处 await 调用。
 *
 * 为什么不用 confirm/prompt：浏览器自带的弹窗是系统样式，和这本"批注本"长得
 * 两回事——深色系统框、原生按钮、在 Electron/窄窗里还可能被拦。更要紧的是它
 * 不能写清后果（"文献本身不会被删"这种话塞进 window.confirm 里很难看）。
 *
 *   const yes = await confirmBox({ title: '删除会话', body: '…', ok: '删除', danger: true })
 *   const name = await inputBox({ title: '重命名会话', value: c.title })
 *
 * 返回值：confirmBox → true / false；inputBox → 字符串 / null（取消）。
 * Esc 与点遮罩 = 取消，Enter = 确定。danger 时确定键用朱红。
 */
import { reactive } from 'vue'

export const dlg = reactive({
  open: false, kind: 'confirm', title: '', body: '', ok: '确定', cancel: '取消',
  value: '', placeholder: '', danger: false, _done: null,
})

function open(opts) {
  return new Promise(resolve => {
    Object.assign(dlg, {
      open: true, kind: 'confirm', body: '', ok: '确定', cancel: '取消',
      value: '', placeholder: '', danger: false, ...opts, _done: resolve,
    })
  })
}
function settle(result) {
  const done = dlg._done
  dlg.open = false
  dlg._done = null
  if (done) done(result)
}

export const confirmBox = opts => open({ ...opts, kind: 'confirm' })
export const inputBox = opts => open({ ...opts, kind: 'input' })

export function dlgOk() {
  settle(dlg.kind === 'input' ? dlg.value : true)
}
export function dlgCancel() { settle(dlg.kind === 'input' ? null : false) }
