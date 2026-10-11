import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './i18n' // Initialize i18n
import App from './App.tsx'

// A tab left open across an update still asks for the old build's chunk names,
// which are gone: a lazily loaded panel then waits forever. Reload into the new
// build instead — at most once a minute, so a genuinely missing chunk can't
// loop.
window.addEventListener('vite:preloadError', (event) => {
  let last = 0
  try {
    last = Number(sessionStorage.getItem('chunk-reload-at')) || 0
    if (Date.now() - last < 60_000) return
    sessionStorage.setItem('chunk-reload-at', String(Date.now()))
  } catch {
    // No storage: still reload; the browser's own error shows if it repeats.
  }
  event.preventDefault()
  window.location.reload()
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
