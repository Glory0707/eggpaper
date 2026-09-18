import { reactive } from 'vue'
import { t } from './i18n'

export const dlg = reactive({
  open: false, kind: 'confirm', title: '', body: '', ok: '', cancel: '',
  value: '', placeholder: '', danger: false, _done: null,
})

function open(opts) {
  return new Promise(resolve => {
    Object.assign(dlg, {
      open: true, kind: 'confirm', body: '', ok: t('确定'), cancel: t('取消'),
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
