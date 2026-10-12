import {editInterviewFields} from '@/shared/api/runs';
import {Button, IconButton, TextArea, TextField, StatusText} from '@/shared/ui';
import {type ReactNode, useId, useState} from 'react';
import {SETUP_ACTIONS_CLASSES} from '@/shared/ui/classes';
import {
  type EditedSpecFields,
  type InferredRunSpec,
  applyEditedInterviewFields,
  buildInterviewFieldsPayload,
} from '@/shared/lib/run_spec';
import {errorMessage} from '@/shared/lib/errors';

function SpecRow({label, children}: {label: string; children: ReactNode}) {
  return (
    <div className="reference-spec-row block text-base">
      <dt className="mb-3.5 text-[1.18rem] font-bold text-cosci-fg">
        {label}:
      </dt>
      <dd className="m-0 leading-[1.45] text-cosci-fg">{children}</dd>
    </div>
  );
}

function SpecList({label, values}: {label: string; values: string[]}) {
  return (
    <SpecRow label={label}>
      <ul className="reference-spec-list m-0 grid list-disc gap-3 pl-5">
        {values.map(value => (
          <li key={value}>{value}</li>
        ))}
      </ul>
    </SpecRow>
  );
}

function SpecSummary({spec}: {spec: InferredRunSpec}) {
  return (
    <dl className="reference-setup-grid m-0 grid gap-6">
      <SpecRow label="Research Challenge">{spec.goal}</SpecRow>
      <SpecList label="Focus Area" values={spec.attributes} />
      <SpecList label="Preferences" values={spec.requirements} />
      {spec.criteria.length > 0 && (
        <SpecList label="Criteria" values={spec.criteria} />
      )}
    </dl>
  );
}

// Name unlabeled entry inputs and remove controls through aria-label.
function EditableListRow({
  label,
  value,
  index,
  onChange,
  onRemove,
}: {
  label: string;
  value: string;
  index: number;
  onChange: (value: string) => void;
  onRemove: () => void;
}) {
  const removeLabel = value.trim()
    ? `Remove "${value}" from ${label}`
    : `Remove empty ${label} entry`;
  return (
    <div className="flex items-center gap-2">
      <TextField
        type="text"
        aria-label={`${label} item ${index + 1}`}
        value={value}
        onChange={e => onChange(e.target.value)}
      />
      <IconButton icon="close" label={removeLabel} onClick={onRemove} />
    </div>
  );
}

function EditableList({
  label,
  values,
  onChange,
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  function updateAt(index: number, value: string) {
    onChange(values.map((entry, i) => (i === index ? value : entry)));
  }
  function removeAt(index: number) {
    onChange(values.filter((_, i) => i !== index));
  }
  return (
    <fieldset className="m-0 grid gap-2.5 border-0 p-0">
      <legend className="text-[1.18rem] font-bold text-cosci-fg">
        {label}
      </legend>
      {values.map((value, index) => (
        <EditableListRow
          key={index}
          label={label}
          value={value}
          index={index}
          onChange={v => updateAt(index, v)}
          onRemove={() => removeAt(index)}
        />
      ))}
      <Button
        variant="text"
        size="sm"
        icon="add"
        layoutClassName="w-fit -ml-2"
        onClick={() => onChange([...values, ''])}
      >
        Add {label.toLowerCase()}
      </Button>
    </fieldset>
  );
}

interface SpecFieldsFormProps {
  values: EditedSpecFields;
  onChange: (patch: Partial<EditedSpecFields>) => void;
  onSave: () => void;
  onCancel: () => void;
  canSave: boolean;
  isSaving: boolean;
  error: string | null;
}

function SpecFieldsFormFields({
  values,
  onChange,
}: Pick<SpecFieldsFormProps, 'values' | 'onChange'>) {
  const goalId = useId();
  return (
    <>
      <div className="grid gap-2">
        <label
          htmlFor={goalId}
          className="text-[1.18rem] font-bold text-cosci-fg"
        >
          Research Challenge
        </label>
        <TextArea
          id={goalId}
          layoutClassName="min-h-[6rem] resize-y leading-[1.45]"
          value={values.goal}
          onChange={e => onChange({goal: e.target.value})}
        />
      </div>
      <EditableList
        label="Focus Area"
        values={values.attributes}
        onChange={attributes => onChange({attributes})}
      />
      <EditableList
        label="Preferences"
        values={values.requirements}
        onChange={requirements => onChange({requirements})}
      />
    </>
  );
}

// Keep failed saves open so the scientist does not lose their edits.
function SpecFieldsFormActions(
  props: Pick<
    SpecFieldsFormProps,
    'onSave' | 'onCancel' | 'canSave' | 'isSaving' | 'error'
  >,
) {
  const {onSave, onCancel, canSave, isSaving, error} = props;
  return (
    <>
      {error && <StatusText tone="danger">{error}</StatusText>}
      <div className={SETUP_ACTIONS_CLASSES}>
        <Button variant="outlined" onClick={onCancel} disabled={isSaving}>
          Cancel
        </Button>
        <Button onClick={onSave} disabled={!canSave || isSaving}>
          {isSaving ? 'Saving...' : 'Save'}
        </Button>
      </div>
    </>
  );
}

function SpecFieldsForm(props: SpecFieldsFormProps) {
  return (
    <div className="reference-spec-edit-form grid gap-5">
      <SpecFieldsFormFields values={props.values} onChange={props.onChange} />
      <SpecFieldsFormActions {...props} />
    </div>
  );
}

export type SpecFieldsEditor = ReturnType<typeof useSpecFieldsEditor>;

function formValues(spec: InferredRunSpec): EditedSpecFields {
  return {
    goal: spec.goal,
    attributes: [...spec.attributes],
    requirements: [...spec.requirements],
  };
}

export function useSpecFieldsEditor(
  spec: InferredRunSpec,
  onFieldsChange: (patch: Partial<InferredRunSpec>) => void,
) {
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<EditedSpecFields>(() =>
    formValues(spec),
  );
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (!spec.interviewId) return;
    setIsSaving(true);
    setError(null);
    try {
      const interview = await editInterviewFields(
        spec.interviewId,
        buildInterviewFieldsPayload(values),
      );
      onFieldsChange(applyEditedInterviewFields(interview));
      setEditing(false);
    } catch (err) {
      setError(errorMessage(err, 'Could not save changes.'));
    } finally {
      setIsSaving(false);
    }
  }

  return {
    editing,
    values,
    isSaving,
    error,
    canSave: values.goal.trim().length > 0,
    onValuesChange: (patch: Partial<EditedSpecFields>) =>
      setValues(current => ({...current, ...patch})),
    startEditing: () => {
      setValues(formValues(spec));
      setError(null);
      setEditing(true);
    },
    cancelEditing: () => setEditing(false),
    save: () => void save(),
  };
}

// Adopt server-returned fields after saving because the server trims list
// entries.
export function SpecFieldsSection({
  spec,
  editor,
}: {
  spec: InferredRunSpec;
  editor: SpecFieldsEditor;
}) {
  if (!editor.editing) return <SpecSummary spec={spec} />;
  return (
    <SpecFieldsForm
      values={editor.values}
      onChange={editor.onValuesChange}
      onSave={editor.save}
      onCancel={editor.cancelEditing}
      canSave={editor.canSave}
      isSaving={editor.isSaving}
      error={editor.error}
    />
  );
}
