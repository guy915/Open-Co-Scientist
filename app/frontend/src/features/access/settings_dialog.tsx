import {type RefObject, useEffect, useRef, useState} from 'react';
import {Icon, type IconName} from '@/shared/ui/icon';
import {
  Button,
  StatusText,
  cardClasses,
  Dialog,
  DIALOG_TITLE_CLASSES,
  IconButton,
  SectionNav,
  SegmentedControl,
  Select,
  TextField,
  ExternalLink,
} from '@/shared/ui';
import {
  type ByokProvider,
  type ModelChoice,
  type ModelTier,
  BYOK_PROVIDERS,
  fallbackProvider,
  getStoredApiKey,
  getStoredApiProvider,
  getStoredModel,
  getStoredCustomModel,
  setStoredCustomModel,
  keyedProviders,
  setStoredApiKey,
  setStoredApiProvider,
  setStoredModel,
} from '@/shared/lib/client_id';
import {type Mode, useTheme} from '@/shared/hooks/theme_context';
import {
  type ByokModelCatalog,
  type FreeUsage,
  fetchByokModelCatalog,
  fetchFreeUsage,
  validateCustomModel,
} from '@/shared/api/system';
import {joinClasses, SETTINGS_FIELD_LABEL_CLASSES} from '@/shared/ui/classes';
import {SETTINGS_SECTIONS, type SettingsSection} from './settings_sections';
export {SETTINGS_SECTIONS, type SettingsSection} from './settings_sections';

const CARD_CLASSES = cardClasses({tone: 'raised', size: 'panel'});
const CARD_TITLE_CLASSES = 'm-0 mb-4 font-gsans text-[1.05rem] font-medium';
const HINT_CLASSES = 'm-0 mt-2 text-[0.78rem] text-cosci-muted';

// Free-text credentials commit on blur/Enter; selections commit whole values
// immediately. Saving silently avoids covering the page with redundant
// confirmation. The field edits the key of the provider being viewed.
function useApiKeyField() {
  const [provider, setProvider] = useState<ByokProvider>(getStoredApiProvider);
  const [apiKey, setApiKey] = useState(() => getStoredApiKey(provider));
  const [savedProviders, setSavedProviders] = useState(keyedProviders);

  function onSave() {
    if (apiKey.trim() === getStoredApiKey(provider)) return;
    setStoredApiKey(apiKey, provider);
    setApiKey(getStoredApiKey(provider));
    setSavedProviders(keyedProviders());
  }

  function onProviderChange(next: ByokProvider) {
    if (next === provider) return;
    setStoredApiProvider(next);
    setProvider(next);
    setApiKey(getStoredApiKey(next));
  }

  return {
    apiKey,
    onApiKeyChange: setApiKey,
    provider,
    onProviderChange,
    onSave,
    savedProviders,
  };
}

function SettingsDialogHeader({
  onClose,
  closeRef,
}: {
  onClose: () => void;
  closeRef: RefObject<HTMLButtonElement | null>;
}) {
  return (
    <header className="flex items-center justify-between gap-4">
      <h2 className={DIALOG_TITLE_CLASSES}>Settings</h2>
      <IconButton
        ref={closeRef}
        size="md"
        icon="close"
        label="Close settings"
        tooltipPlacement="left"
        onClick={onClose}
      />
    </header>
  );
}

interface SettingsDialogProps {
  // `null` closes the dialog; the last section stays drawn while it fades.
  section: SettingsSection | null;
  onSectionChange: (section: SettingsSection) => void;
  onClose: () => void;
}

export function SettingsDialog({
  section,
  onSectionChange,
  onClose,
}: SettingsDialogProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [shown, setShown] = useState<SettingsSection>(section ?? 'appearance');
  if (section && section !== shown) setShown(section);

  return (
    <Dialog
      open={section !== null}
      onClose={onClose}
      label="Settings"
      size="lg"
      initialFocusRef={closeRef}
    >
      <SettingsDialogHeader onClose={onClose} closeRef={closeRef} />
      <SettingsBody section={shown} onSectionChange={onSectionChange} />
    </Dialog>
  );
}

// Mounted per opening, so the key field rereads browser storage each time.
function SettingsBody({
  section,
  onSectionChange,
}: {
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
}) {
  const theme = useTheme();
  const apiKeyField = useApiKeyField();
  return (
    <div className="mt-5 grid min-h-0 flex-1 grid-cols-[13rem_minmax(0,1fr)] gap-6 phone:mt-3.5 phone:grid-cols-[minmax(0,1fr)] phone:grid-rows-[auto_minmax(0,1fr)] phone:gap-4">
      <SettingsNav section={section} onSectionChange={onSectionChange} />
      <div
        className="grid min-h-0 gap-4 overflow-y-auto pr-1 [align-content:start]"
        role="region"
        aria-label="Settings content"
        // Firefox tabs to scroll areas; include this stop in the focus trap.
        tabIndex={0}
      >
        {section === 'appearance' && (
          <AppearanceSection mode={theme.mode} setMode={theme.setMode} />
        )}
        {section === 'model' && <ModelSection {...apiKeyField} />}
      </div>
    </div>
  );
}

const THEME_MODES: {mode: Mode; icon: IconName; label: string}[] = [
  {mode: 'system', icon: 'computer', label: 'System'},
  {mode: 'light', icon: 'light_mode', label: 'Light'},
  {mode: 'dark', icon: 'dark_mode', label: 'Dark'},
];

export function AppearanceSection({
  mode,
  setMode,
}: {
  mode: Mode;
  setMode: (mode: Mode) => void;
}) {
  return (
    <section className={CARD_CLASSES}>
      <h3 className={CARD_TITLE_CLASSES}>Theme</h3>
      <SegmentedControl
        label="Theme"
        value={mode}
        onChange={setMode}
        options={THEME_MODES.map(option => ({
          value: option.mode,
          label: option.label,
          icon: option.icon,
        }))}
      />
    </section>
  );
}

function ApiKeyHint({provider}: {provider: ByokProvider}) {
  const {url, article} = PROVIDER_KEY_PAGES[provider];
  return (
    <p className={HINT_CLASSES}>
      <ExternalLink
        className="inline-flex items-center gap-1 text-[0.82rem] font-medium text-cosci-blue no-underline focus-visible:underline hover:underline"
        href={url}
      >
        Get {article} {PROVIDER_LABELS[provider]} API key
        <Icon className="text-[0.95rem]" name="open_in_new" />
      </ExternalLink>
    </p>
  );
}

export function ModelSection({
  apiKey,
  onApiKeyChange,
  provider,
  onProviderChange,
  onSave,
  savedProviders,
}: {
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  provider: ByokProvider;
  onProviderChange: (value: ByokProvider) => void;
  onSave: () => void;
  savedProviders: ByokProvider[];
}) {
  const modelFields = useModelFields(provider, savedProviders);
  return (
    <div className="grid gap-4">
      <section className={CARD_CLASSES} aria-labelledby={LABEL_ID}>
        {/* The heading also names the provider menu. */}
        <h3 id={LABEL_ID} className={CARD_TITLE_CLASSES}>
          Provider
        </h3>
        <ProviderSelect
          provider={provider}
          savedProviders={savedProviders}
          onChange={onProviderChange}
        />
        <label
          className={joinClasses('mt-3.5 block', SETTINGS_FIELD_LABEL_CLASSES)}
          htmlFor="cosci-settings-api-key"
        >
          {PROVIDER_LABELS[provider]} API key
        </label>
        <TextField
          id="cosci-settings-api-key"
          type="password"
          autoComplete="off"
          placeholder={`Paste your ${PROVIDER_LABELS[provider]} API key`}
          value={apiKey}
          onChange={event => onApiKeyChange(event.target.value)}
          onBlur={onSave}
          onKeyDown={event => {
            if (event.key === 'Enter') onSave();
          }}
        />
        <ApiKeyHint provider={provider} />
        <p className={HINT_CLASSES}>
          Keys are saved on this browser. Clear a key’s field to remove it.
          Existing background runs keep their credentials.
        </p>
      </section>
      <section className={CARD_CLASSES} aria-labelledby="cosci-models-title">
        <h3 id="cosci-models-title" className={CARD_TITLE_CLASSES}>
          Model
        </h3>
        <ModelSelectors
          hasKey={savedProviders.length > 0}
          fields={modelFields}
        />
      </section>
    </div>
  );
}

export function SettingsNav({
  section,
  onSectionChange,
}: {
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
}) {
  return (
    <SectionNav
      label="Settings sections"
      value={section}
      onChange={onSectionChange}
      items={SETTINGS_SECTIONS.map(item => ({
        value: item.section,
        label: item.label,
        icon: item.icon,
      }))}
    />
  );
}

const TIER_LABELS: Record<ModelTier, string> = {
  supervisor: 'Supervisor model',
  worker: 'Worker model',
};

// The provider chooser already names the provider, so model labels omit its
// repeated prefix.
export function modelLabel(model: string): string {
  const slash = model.indexOf('/');
  return slash < 0 ? model : model.slice(slash + 1);
}

const OTHER_PREFIX = 'other:';

function otherOption(provider: ByokProvider): string {
  return `${OTHER_PREFIX}${provider}`;
}

function ModelSelect({
  tier,
  value,
  options,
  groupOf,
  disabled,
  onChange,
  custom,
  onCustomChange,
}: {
  tier: ModelTier;
  value: string;
  options: string[];
  groupOf?: (model: string) => string;
  disabled: boolean;
  onChange: (model: string) => void;
  custom: ModelChoice | null;
  onCustomChange: (model: string) => void;
}) {
  const triggerId = `cosci-settings-${tier}-model`;
  const labelId = `${triggerId}-label`;
  return (
    <div className="min-w-0">
      <label
        id={labelId}
        className={joinClasses('block', SETTINGS_FIELD_LABEL_CLASSES)}
        htmlFor={triggerId}
      >
        {TIER_LABELS[tier]}
      </label>
      <Select
        value={value}
        options={options}
        optionLabel={model =>
          model.startsWith(OTHER_PREFIX) ? 'Other' : modelLabel(model)
        }
        groupOf={groupOf}
        truncate
        name={TIER_LABELS[tier]}
        triggerId={triggerId}
        labelId={labelId}
        disabled={disabled}
        align={tier === 'worker' ? 'end' : 'start'}
        onChange={onChange}
      />
      {custom && (
        <CustomModelField
          key={`${tier}:${custom.provider}`}
          tier={tier}
          choice={custom}
          onChange={onCustomChange}
        />
      )}
    </div>
  );
}

function CustomModelField({
  tier,
  choice,
  onChange,
}: {
  tier: ModelTier;
  choice: ModelChoice;
  onChange: (model: string) => void;
}) {
  const [status, setStatus] = useState<'idle' | 'checking' | 'valid' | 'error'>(
    'idle',
  );
  const [error, setError] = useState('');
  const generation = useRef(0);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  const savedKey = getStoredApiKey(choice.provider);
  useEffect(() => {
    generation.current++;
    setStatus('idle');
  }, [savedKey]);
  async function check() {
    const current = ++generation.current;
    const key = getStoredApiKey(choice.provider);
    if (!choice.model.trim() || !key) {
      setStatus('error');
      setError(
        key ? 'Enter a model ID' : 'Save your API key for this provider first',
      );
      return;
    }
    setStatus('checking');
    try {
      const result = await validateCustomModel(
        choice.provider,
        choice.model,
        key,
      );
      if (current !== generation.current) return;
      if (!result.supported)
        throw new Error(result.error ?? 'This model is unsupported');
      onChange(result.model);
      setStatus('valid');
    } catch (cause) {
      if (current !== generation.current) return;
      setError(
        cause instanceof Error
          ? cause.message
          : 'The model could not be checked',
      );
      setStatus('error');
    }
  }
  const fieldId = `cosci-settings-${tier}-custom-model`;
  const statusId = `${fieldId}-status`;
  return (
    <form
      className="ui-motion-enter mt-3 grid gap-2"
      onSubmit={event => {
        event.preventDefault();
        void check();
      }}
    >
      <label className={SETTINGS_FIELD_LABEL_CLASSES} htmlFor={fieldId}>
        Custom {tier} model ID ({PROVIDER_LABELS[choice.provider]})
      </label>
      <TextField
        id={fieldId}
        value={choice.model}
        placeholder="provider/model-name"
        autoComplete="off"
        spellCheck={false}
        aria-describedby={statusId}
        aria-invalid={status === 'error'}
        onBlur={() => {
          void check();
        }}
        onChange={event => {
          generation.current++;
          setStatus('idle');
          onChange(event.target.value);
        }}
      />
      <Button
        type="submit"
        variant="outlined"
        size="sm"
        disabled={status === 'checking'}
      >
        Check model
      </Button>
      <div id={statusId} aria-live="polite" role="status">
        {status === 'checking' && <StatusText>Checking…</StatusText>}
        {status === 'valid' && (
          <StatusText>
            <Icon name="check" /> Model checked
          </StatusText>
        )}
        {status === 'error' && <StatusText tone="danger">{error}</StatusText>}
      </div>
    </form>
  );
}

// Until catalog or quota reads succeed, disable selections and keep unknown
// usage silent rather than guessing.
function useModelSettingsData() {
  const [catalog, setCatalog] = useState<ByokModelCatalog | null>(null);
  const [freeUsage, setFreeUsage] = useState<FreeUsage | null>(null);
  useEffect(() => {
    let live = true;
    fetchByokModelCatalog()
      .then(next => live && setCatalog(next))
      .catch(() => undefined);
    fetchFreeUsage()
      .then(next => live && setFreeUsage(next))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return {catalog, freeUsage};
}

interface ModelGroup {
  provider: ByokProvider;
  models: string[];
}

function storedChoices(): Record<ModelTier, ModelChoice | null> {
  return {
    worker: getStoredModel('worker'),
    supervisor: getStoredModel('supervisor'),
  };
}

// Choices naming a model the catalog no longer offers would be refused by the
// backend, so drop them once the catalog is known.
function pruneRetiredChoices(catalog: ByokModelCatalog) {
  for (const tier of ['worker', 'supervisor'] as const) {
    const choice = getStoredModel(tier);
    if (
      choice &&
      !choice.custom &&
      !catalog[choice.provider]?.includes(choice.model)
    ) {
      setStoredModel(tier, null);
    }
  }
}

// Every provider with a saved key contributes its models; without keys the
// viewed provider's models can still be chosen ahead of time.
export function useModelFields(
  provider: ByokProvider,
  savedProviders: ByokProvider[],
) {
  const {catalog, freeUsage} = useModelSettingsData();
  const [choices, setChoices] = useState(storedChoices);
  useEffect(() => {
    if (catalog) pruneRetiredChoices(catalog);
    setChoices(storedChoices());
  }, [catalog, savedProviders]);
  const groups: ModelGroup[] = (
    savedProviders.length > 0 ? savedProviders : [provider]
  )
    .map(name => ({provider: name, models: catalog?.[name] ?? []}))
    .filter(group => group.models.length > 0);
  const options = groups.flatMap(group => [
    ...group.models,
    otherOption(group.provider),
  ]);
  const groupFor = (model: string) =>
    groups.find(
      group =>
        group.models.includes(model) || otherOption(group.provider) === model,
    )?.provider;

  // An unusable or empty choice means the provider default, not a missing
  // selection; the supervisor then follows the worker's provider.
  const usable = (choice: ModelChoice | null) =>
    choice &&
    (choice.custom
      ? groups.some(group => group.provider === choice.provider)
      : groupFor(choice.model) === choice.provider)
      ? choice
      : null;
  const worker = usable(choices.worker);
  const workerProvider =
    worker?.provider ?? fallbackProvider(savedProviders, provider);
  const supervisor = usable(choices.supervisor);
  const defaultOf = (name: ByokProvider) => catalog?.[name]?.[0] ?? '';
  const shown: Record<ModelTier, string> = {
    worker: worker?.custom
      ? otherOption(worker.provider)
      : (worker?.model ?? defaultOf(workerProvider)),
    supervisor: supervisor?.custom
      ? otherOption(supervisor.provider)
      : (supervisor?.model ??
        defaultOf(supervisor?.provider ?? workerProvider)),
  };

  // An unchosen tier only displays a fallback that follows the worker, so
  // pin what it shows before the other tier moves provider.
  function onModelChange(tier: ModelTier, model: string) {
    const owner = groupFor(model);
    if (!owner) return;
    const selected: ModelChoice = model.startsWith(OTHER_PREFIX)
      ? {
          provider: owner,
          model: getStoredCustomModel(tier, owner),
          custom: true,
        }
      : {provider: owner, model};
    const next = {...choices, [tier]: selected};
    for (const name of ['worker', 'supervisor'] as const) {
      const pinned = shown[name] && groupFor(shown[name]);
      if (!usable(next[name]) && pinned) {
        next[name] = {provider: pinned, model: shown[name]};
      }
      setStoredModel(name, next[name]);
    }
    setChoices(next);
  }

  return {
    options,
    groupOf:
      groups.length > 1
        ? (model: string) => PROVIDER_LABELS[groupFor(model) ?? provider]
        : undefined,
    ...shown,
    custom: {
      worker: worker?.custom ? worker : null,
      supervisor: supervisor?.custom ? supervisor : null,
    },
    onCustomChange(tier: ModelTier, model: string) {
      const selected = choices[tier];
      if (!selected?.custom) return;
      const next = {...selected, model};
      setStoredCustomModel(tier, selected.provider, model);
      setStoredModel(tier, next);
      setChoices(previous => ({...previous, [tier]: next}));
    },
    freeUsage,
    onModelChange,
  };
}

export type ModelFields = ReturnType<typeof useModelFields>;

// Offline keyless runs spend no free allowance; hide quota text until the real-
// backed count is known.
function FreeUsageNote({usage}: {usage: FreeUsage | null}) {
  if (!usage?.enforced) return null;
  const count =
    usage.limit === null
      ? ''
      : `, ${usage.remaining} of ${usage.limit} left today`;
  return (
    <p className={HINT_CLASSES} role="status">
      Free usage: Express runs only{count}.
    </p>
  );
}

// Model preferences can be chosen before a key exists; headers transmit them
// only with a key.
export function ModelSelectors({
  hasKey,
  fields,
}: {
  hasKey: boolean;
  fields: ModelFields;
}) {
  const disabled = fields.options.length === 0;
  return (
    <>
      <div className="mt-3.5 grid grid-cols-2 gap-3 phone:grid-cols-[minmax(0,1fr)]">
        {(['supervisor', 'worker'] as const).map(tier => (
          <ModelSelect
            key={tier}
            tier={tier}
            value={fields[tier]}
            options={fields.options}
            groupOf={fields.groupOf}
            disabled={disabled}
            onChange={model => fields.onModelChange(tier, model)}
            custom={fields.custom[tier]}
            onCustomChange={model => fields.onCustomChange(tier, model)}
          />
        ))}
      </div>
      {!hasKey && <FreeUsageNote usage={fields.freeUsage} />}
    </>
  );
}

export const PROVIDER_LABELS: Record<ByokProvider, string> = {
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  gemini: 'Gemini',
  openai: 'OpenAI',
  openrouter: 'OpenRouter',
};

// Store the article because pronunciation, not the first letter, determines it;
// derive the displayed provider name from the shared labels.
export const PROVIDER_KEY_PAGES: Record<
  ByokProvider,
  {url: string; article: 'a' | 'an'}
> = {
  anthropic: {
    url: 'https://platform.claude.com/settings/keys',
    article: 'an',
  },
  deepseek: {url: 'https://platform.deepseek.com/api_keys', article: 'a'},
  gemini: {url: 'https://aistudio.google.com/apikey', article: 'a'},
  openai: {url: 'https://platform.openai.com/api-keys', article: 'an'},
  openrouter: {url: 'https://openrouter.ai/settings/keys', article: 'an'},
};

const TRIGGER_ID = 'cosci-settings-provider';
const LABEL_ID = 'cosci-settings-provider-label';

export function ProviderSelect({
  provider,
  savedProviders,
  onChange,
}: {
  provider: ByokProvider;
  savedProviders: ByokProvider[];
  onChange: (provider: ByokProvider) => void;
}) {
  return (
    <Select
      value={provider}
      options={BYOK_PROVIDERS}
      optionLabel={option => PROVIDER_LABELS[option]}
      optionNote={option => (savedProviders.includes(option) ? 'Saved' : null)}
      name="Provider"
      triggerId={TRIGGER_ID}
      labelId={LABEL_ID}
      onChange={onChange}
    />
  );
}
