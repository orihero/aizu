import type { InterviewQuestion } from '@/shared/types/domain';
import { FIELD_CLASS } from './formStyles';
import { PlatformPickerQuestion } from './PlatformPickerQuestion';
import type { AnswerState } from './interviewAnswers';

interface QuestionFieldProps {
  readonly question: InterviewQuestion;
  readonly answer: AnswerState;
  readonly onChange: (next: AnswerState) => void;
}

const CHOICE_CLASS =
  'flex items-start gap-2.5 rounded-tile border border-border bg-surface px-3.5 py-2.5 text-sm text-text transition hover:border-brand/40 cursor-pointer';
const OTHER_CLASS = `${FIELD_CLASS} mt-2`;

/** Renders the right control for a question type, fully controlled via AnswerState. */
export function QuestionField({ question, answer, onChange }: QuestionFieldProps) {
  if (question.type === 'platforms') {
    return (
      <PlatformPickerQuestion
        suggested={question.suggested ?? []}
        selected={answer.values}
        onChange={(next) => { onChange({ values: [...next], customText: '' }); }}
      />
    );
  }

  if (question.type === 'text') {
    return (
      <textarea
        rows={3}
        value={answer.values[0] ?? ''}
        placeholder={question.placeholder}
        onChange={(e) => { onChange({ values: [e.target.value], customText: '' }); }}
        className={FIELD_CLASS}
      />
    );
  }

  // single | multi — radio/checkbox over options, with an optional "Other" field.
  const isMulti = question.type === 'multi';
  const options = question.options ?? [];

  function toggle(value: string) {
    if (isMulti) {
      const next = answer.values.includes(value)
        ? answer.values.filter((v) => v !== value)
        : [...answer.values, value];
      onChange({ ...answer, values: next });
    } else {
      onChange({ ...answer, values: [value] });
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {options.map((opt) => (
        <label key={opt.value} className={CHOICE_CLASS}>
          <input
            type={isMulti ? 'checkbox' : 'radio'}
            name={question.id}
            checked={answer.values.includes(opt.value)}
            onChange={() => { toggle(opt.value); }}
            className="mt-0.5 size-4 accent-brand"
          />
          <span>
            <span className="font-semibold">{opt.label}</span>
            {opt.hint ? <span className="block text-[12px] text-text-muted">{opt.hint}</span> : null}
          </span>
        </label>
      ))}
      {question.allowCustom ? (
        <input
          type="text"
          value={answer.customText}
          placeholder="Other (please specify)"
          onChange={(e) => { onChange({ ...answer, customText: e.target.value }); }}
          className={OTHER_CLASS}
        />
      ) : null}
    </div>
  );
}
