import { useState } from 'react';
import { ArrowRight, MessageCircleQuestion } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody } from '@/shared/ui/Card';
import type { InterviewAnswer, InterviewQuestion } from '@/shared/types/domain';
import { LABEL_CLASS } from './formStyles';
import { QuestionField } from './QuestionField';
import { answerToText, initialAnswer, isAnswered, type AnswerState } from './interviewAnswers';

/** The result of a round: the readable transcript + the platforms chosen this
 *  round (null when the round had no platforms question). */
export interface InterviewRoundResult {
  readonly answers: readonly InterviewAnswer[];
  readonly platforms: readonly string[] | null;
}

interface CampaignInterviewProps {
  readonly questions: readonly InterviewQuestion[];
  readonly round: number;
  /** Submit answers and ask for the next round. */
  readonly onContinue: (result: InterviewRoundResult) => void;
  /** Submit answers and skip straight to drafting the campaign. */
  readonly onFinish: (result: InterviewRoundResult) => void;
}

function collect(
  questions: readonly InterviewQuestion[],
  answers: Record<string, AnswerState>,
): InterviewRoundResult {
  const transcript: InterviewAnswer[] = [];
  for (const q of questions) {
    const text = answerToText(q, answers[q.id] ?? { values: [], customText: '' });
    if (text.trim()) transcript.push({ question: q.prompt, answer: text });
  }
  const platformsQuestion = questions.find((q) => q.type === 'platforms');
  const platforms = platformsQuestion
    ? [...(answers[platformsQuestion.id]?.values ?? [])]
    : null;
  return { answers: transcript, platforms };
}

/**
 * One round of the conversational interview: renders the AI's questions (mixed
 * types), collects answers, and either asks for the next round or finishes early.
 * Mounted with a `key={round}` by the wizard so each round seeds fresh answer
 * state. The in-flight / drafting states are owned by the wizard.
 */
export function CampaignInterview({ questions, round, onContinue, onFinish }: CampaignInterviewProps) {
  const [answers, setAnswers] = useState<Record<string, AnswerState>>(() =>
    Object.fromEntries(questions.map((q) => [q.id, initialAnswer(q)])),
  );

  const allAnswered = questions.every((q) => isAnswered(q, answers[q.id] ?? { values: [], customText: '' }));

  function patch(id: string, next: AnswerState) {
    setAnswers((prev) => ({ ...prev, [id]: next }));
  }

  return (
    <Card>
      <CardBody className="flex flex-col gap-6 p-6">
        <div className="flex items-start gap-3">
          <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand">
            <MessageCircleQuestion className="size-5" aria-hidden />
          </span>
          <div>
            <h2 className="text-lg font-extrabold text-text">A few questions</h2>
            <p className="mt-0.5 text-[13px] text-text-muted">
              Answer these so we can target the right people. Round {round}.
            </p>
          </div>
        </div>

        <ol className="flex flex-col gap-6">
          {questions.map((question) => (
            <li key={question.id}>
              <p className={LABEL_CLASS}>{question.prompt}</p>
              {question.help ? (
                <p className="-mt-1 mb-2 text-[12px] text-text-muted">{question.help}</p>
              ) : null}
              <QuestionField
                question={question}
                answer={answers[question.id] ?? { values: [], customText: '' }}
                onChange={(next) => { patch(question.id, next); }}
              />
            </li>
          ))}
        </ol>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
          <button
            type="button"
            onClick={() => { onFinish(collect(questions, answers)); }}
            className="text-[13px] font-bold text-text-muted underline-offset-2 transition hover:text-text hover:underline"
          >
            Skip the rest &amp; draft now
          </button>
          <Button onClick={() => { onContinue(collect(questions, answers)); }} disabled={!allAnswered}>
            Continue
            <ArrowRight className="size-4" aria-hidden />
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
