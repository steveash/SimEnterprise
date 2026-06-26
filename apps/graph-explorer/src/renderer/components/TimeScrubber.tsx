import { useStore } from '../store.js'

export function TimeScrubber(): JSX.Element {
  const model = useStore((s) => s.model)
  const timeCursor = useStore((s) => s.timeCursor)
  const setTimeCursor = useStore((s) => s.setTimeCursor)
  if (!model) return <></>

  // bound the slider by node created_at extent
  const times = model.nodes.map((n) => Date.parse(n.created_at)).filter((t) => !Number.isNaN(t))
  if (!times.length) return <></>
  const min = Math.min(...times)
  const max = Math.max(...times)
  if (min === max) return <></>
  const value = timeCursor ?? max

  return (
    <div className="time-scrubber">
      <span className="scrub-label">⏱</span>
      <input
        type="range"
        min={min}
        max={max}
        step={Math.max(1, Math.floor((max - min) / 200))}
        value={value}
        onChange={(e) => setTimeCursor(Number(e.target.value))}
      />
      <span className="scrub-time mono">{new Date(value).toISOString().slice(0, 16).replace('T', ' ')}</span>
      <button className="mini-btn" onClick={() => setTimeCursor(null)} disabled={timeCursor === null}>
        live
      </button>
    </div>
  )
}
