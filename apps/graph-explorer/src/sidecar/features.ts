// Feature-op registration manifest. Each feature module calls `registerOps`
// from `./ops.js` at import time; importing it here is what wires it into the
// dispatcher. Keep this file to imports only.
//
import './jobs/index.js'
// import './templates/index.js'
// import './evals/index.js'
export {}
