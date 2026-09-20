// Composes the question browser (left), run controls (top-center), and the
// question detail pane (right) — docs/EXPLORER_EVALS.md §5.
import { QuestionBrowser } from './QuestionBrowser.js'
import { RunControls } from './RunControls.js'
import { QuestionDetail } from './QuestionDetail.js'

export function QuestionsTab(): JSX.Element {
  return (
    <div className="evals-questions">
      <QuestionBrowser />
      <div className="evals-center">
        <RunControls />
      </div>
      <QuestionDetail />
    </div>
  )
}
