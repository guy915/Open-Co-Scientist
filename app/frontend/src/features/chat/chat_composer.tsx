import {
  type FormEvent,
  type ReactNode,
  useLayoutEffect,
  useRef,
  type ChangeEvent,
  useEffect,
  useState,
  type RefObject,
} from 'react';
import {type IconName} from '@/shared/ui/icon';
import {IconButton, Menu, MenuItem, TextArea} from '@/shared/ui';
import {joinClasses, tooltipClassNames} from '@/shared/ui/classes';
import type {Connector, SystemStatus} from '@/shared/api/system';
import {useSystemStatus} from '@/shared/hooks/system_status_context';
import {useLaunchStatus} from '@/shared/hooks/launch_status_context';
import {LAUNCH_NOTICE_ID} from '@/shared/ui/launch_status_banner';

export interface ComposerProps {
  input: string;
  setInput: (value: string) => void;
  // Once a conversation has a message, the composer asks for the next one.
  inConversation?: boolean;
  busy: boolean;
  large?: boolean;
  autoFocus?: boolean;
  connectors?: ConnectorToggleProps;
  onSubmit: (e: FormEvent<HTMLFormElement>, files: File[]) => void;
  stoppable?: boolean;
  onStop?: () => void;
  aboveInput?: ReactNode;
}

const DEFAULT_CONNECTORS: ConnectorToggleProps = {
  pubmedEnabled: true,
  webSearchEnabled: true,
};

// Keep typing while replies stream; busy gates sending, while stoppable
// permits interruption.
export function Composer({
  input,
  setInput,
  busy,
  inConversation = false,
  large = false,
  autoFocus = false,
  connectors = DEFAULT_CONNECTORS,
  onSubmit,
  stoppable = false,
  onStop,
  aboveInput,
}: ComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const {attachments, onFilesChanged, removeAttachment, clearAttachments} =
    useComposerAttachments();
  const {connectorsOpen, setConnectorsOpen, sourceControlsRef} =
    useConnectorsMenu();
  const {status} = useLaunchStatus();
  const paused = status?.paused ?? false;
  const submitDisabled = !input.trim() || busy || paused;
  const placeholder = inConversation
    ? 'Ask Co-Scientist'
    : 'Start a new research goal to begin';

  // Measure programmatic fills as well as typing. The height eases from the
  // last measured size; a scrollbar appears only once content passes the cap.
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const from = textarea.style.height;
    const cap = large ? 146 : 120;
    textarea.style.height = 'auto';
    const content = textarea.scrollHeight;
    const to = `${Math.min(content, cap)}px`;
    textarea.style.overflowY = content > cap ? 'auto' : 'hidden';
    if (from && from !== to) {
      textarea.style.height = from;
      void textarea.offsetHeight;
    }
    textarea.style.height = to;
  }, [input, large]);

  // Composer elevation is light-only in the reference, so its shadow stays a
  // themed token.
  return (
    <form
      onSubmit={event => {
        if (paused) {
          event.preventDefault();
          return;
        }
        onSubmit(
          event,
          attachments.map(attachment => attachment.file),
        );
        if (input.trim()) clearAttachments();
      }}
      className={joinClasses(
        'reference-composer relative mt-4 min-h-[7.9rem] rounded-4xl border border-cosci-composer-border bg-cosci-composer-bg p-[1.25rem_1.5rem_0.8rem] [box-shadow:var(--cosci-composer-shadow)]',
        input.trim() && 'has-input',
        large &&
          'above-phone:row-6 desktop:row-7 desktop:mt-[clamp(1.25rem,2.4vh,1.9rem)] [@media(min-width:1181px)_and_(max-height:760px)]:min-h-[6.35rem] phone:mt-3.5 phone:min-h-0 phone:pt-3.5 phone:pb-2.5',
        attachments.length > 0 && 'has-attachments !min-h-[13.5rem] !pt-4',
      )}
    >
      {aboveInput}
      <AttachmentStrip attachments={attachments} onRemove={removeAttachment} />
      {/* A native placeholder starts exactly where the caret and typed text
          do. */}
      <div className="relative block min-h-[3.6rem] pb-12">
        <TextArea
          ref={textareaRef}
          rows={1}
          value={input}
          placeholder={placeholder}
          aria-label={placeholder}
          autoFocus={autoFocus}
          variant="bare"
          layoutClassName={joinClasses(
            'relative z-2 block min-h-[2.85rem] resize-none overflow-y-auto leading-6 transition-[height] duration-short ease-standard motion-reduce:transition-none',
            large &&
              '[@media(min-width:1181px)_and_(max-height:760px)]:min-h-[2.65rem]',
          )}
          onChange={event => setInput(event.target.value)}
          onKeyDown={event => {
            if (event.key !== 'Enter' || event.shiftKey) return;
            event.preventDefault();
            // requestSubmit ignores a disabled submit button.
            if (!submitDisabled) event.currentTarget.form?.requestSubmit();
          }}
        />
      </div>
      <div className="reference-composer-actions pointer-events-none absolute right-5 bottom-3 left-5 flex items-end justify-between gap-3">
        <SourceControls
          connectorsOpen={connectorsOpen}
          onToggleConnectors={() => setConnectorsOpen(open => !open)}
          sourceControlsRef={sourceControlsRef}
          fileInputRef={fileInputRef}
          onFilesChanged={onFilesChanged}
          connectors={connectors}
        />
        <IconButton
          type={stoppable ? 'button' : 'submit'}
          icon={stoppable ? 'stop' : 'send'}
          label={stoppable ? 'Stop' : 'Send'}
          tooltip={
            stoppable
              ? 'Stop'
              : paused
                ? (status?.message ?? 'Research is paused.')
                : 'Submit'
          }
          aria-describedby={!stoppable && paused ? LAUNCH_NOTICE_ID : undefined}
          layoutClassName="pointer-events-auto"
          disabled={!stoppable && submitDisabled}
          onClick={stoppable ? onStop : undefined}
        />
      </div>
    </form>
  );
}

interface ComposerAttachment {
  id: string;
  file: File;
  name: string;
  badge: string;
  kind: string;
  isImage: boolean;
  previewUrl: string | null;
}

// Revoke previews on remove, clear and unmount; unreleased object URLs retain
// blobs for the document lifetime.
function revokePreview(attachment: ComposerAttachment): void {
  if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
}

// Mirror attachments in a ref so unmount cleanup sees the latest previews
// without resubscribing.
function useAttachmentPreviewCleanup(attachments: ComposerAttachment[]) {
  const attachmentRef = useRef<ComposerAttachment[]>([]);

  useEffect(() => {
    attachmentRef.current = attachments;
  }, [attachments]);

  useEffect(() => {
    return () => {
      attachmentRef.current.forEach(revokePreview);
    };
  }, []);
}

function useComposerAttachments() {
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  useAttachmentPreviewCleanup(attachments);

  // Reset the file input so picking the same file still fires change.
  function onFilesChanged(e: ChangeEvent<HTMLInputElement>) {
    const nextAttachments = Array.from(e.target.files ?? []).map(file =>
      fileToAttachment(file),
    );
    setAttachments(current => [...current, ...nextAttachments]);
    e.target.value = '';
  }

  function removeAttachment(id: string) {
    setAttachments(current => {
      const attachment = current.find(item => item.id === id);
      if (attachment) revokePreview(attachment);
      return current.filter(item => item.id !== id);
    });
  }

  function clearAttachments() {
    setAttachments(current => {
      current.forEach(revokePreview);
      return [];
    });
  }

  return {attachments, onFilesChanged, removeAttachment, clearAttachments};
}

function AttachmentStrip({
  attachments,
  onRemove,
}: {
  attachments: ComposerAttachment[];
  onRemove: (id: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div
      className="reference-attachment-strip ui-motion-enter-items flex min-w-0 flex-wrap gap-3 pb-5 pointer-events-auto"
      aria-label="Attachments"
    >
      {attachments.map(attachment => (
        <AttachmentCard
          key={attachment.id}
          attachment={attachment}
          onRemove={onRemove}
        />
      ))}
    </div>
  );
}

function AttachmentCard({
  attachment,
  onRemove,
}: {
  attachment: ComposerAttachment;
  onRemove: (id: string) => void;
}) {
  const removeButton = (
    <AttachmentRemoveButton attachment={attachment} onRemove={onRemove} />
  );

  if (attachment.isImage && attachment.previewUrl) {
    return (
      <ImageAttachmentCard
        name={attachment.name}
        previewUrl={attachment.previewUrl}
        removeButton={removeButton}
      />
    );
  }

  return (
    <FileAttachmentCard attachment={attachment} removeButton={removeButton} />
  );
}

function ImageAttachmentCard({
  name,
  previewUrl,
  removeButton,
}: {
  name: string;
  previewUrl: string;
  removeButton: ReactNode;
}) {
  return (
    <div
      className={tooltipClassNames({
        className:
          'reference-attachment-card reference-attachment-card--image group relative box-border grid size-[4.85rem] flex-none items-center overflow-hidden rounded-2xl border-0 bg-cosci-attach-bg p-0 text-cosci-attach-fg',
        placement: 'top',
        wrap: true,
        alignStart: true,
      })}
      data-tooltip={name}
    >
      <img
        src={previewUrl}
        alt={name}
        className="absolute inset-0 size-full rounded-2xl object-cover"
      />
      {removeButton}
    </div>
  );
}

function FileAttachmentCard({
  attachment,
  removeButton,
}: {
  attachment: ComposerAttachment;
  removeButton: ReactNode;
}) {
  return (
    <div
      className={tooltipClassNames({
        className:
          'reference-attachment-card group relative box-border grid h-[4.85rem] w-[13.75rem] flex-none items-center rounded-2xl border-0 bg-cosci-attach-bg py-3.5 pr-13 pl-4 text-cosci-attach-fg',
        placement: 'top',
        wrap: true,
        alignStart: true,
      })}
      data-tooltip={attachment.name}
    >
      <div className="reference-attachment-text grid min-w-0 gap-2">
        <strong className="overflow-hidden text-ellipsis whitespace-nowrap text-base font-medium leading-[1.15]">
          {attachment.name}
        </strong>
        <span className="flex min-w-0 items-center gap-2 text-[0.9rem] leading-[1.2] text-cosci-attach-meta">
          <span className="reference-attachment-extension inline-grid h-[1.35rem] min-w-[1.35rem] place-items-center rounded-sm bg-cosci-attach-badge text-[0.48rem] leading-none font-bold text-cosci-attach-badge-fg">
            {attachment.badge}
          </span>
          {attachment.kind}
        </span>
      </div>
      {removeButton}
    </div>
  );
}

function AttachmentRemoveButton({
  attachment,
  onRemove,
}: {
  attachment: ComposerAttachment;
  onRemove: (id: string) => void;
}) {
  return (
    // Revealed on hover or focus within the card; always shown on touch.
    <IconButton
      variant="elevated"
      icon="close"
      label={`Remove ${attachment.name}`}
      layoutClassName="absolute top-[0.62rem] right-[0.62rem] opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
      onClick={() => onRemove(attachment.id)}
    />
  );
}

function fileToAttachment(file: File): ComposerAttachment {
  const extension = fileExtension(file.name);
  const isImage = file.type.startsWith('image/');
  const suffix = Math.random().toString(36).slice(2);
  return {
    id: `${file.name}-${file.lastModified}-${suffix}`,
    file,
    name: file.name,
    badge: fileBadge(extension),
    kind: fileKind(file, extension),
    isImage,
    previewUrl: isImage ? URL.createObjectURL(file) : null,
  };
}

function fileExtension(name: string) {
  const extension = name.split('.').pop()?.trim();
  return extension ? extension.slice(0, 8).toLowerCase() : '';
}

function fileBadge(extension: string) {
  if (['md', 'mkdn', 'markdown', 'txt'].includes(extension)) return 'TXT';
  return extension ? extension.slice(0, 4).toUpperCase() : 'FILE';
}

// Markdown extensions outrank text MIME types; CSV can retain MIME-first
// classification.
const MARKDOWN_EXTENSIONS = ['md', 'mkdn', 'markdown'];

const EXTENSION_KIND_LABELS: Record<string, string> = {
  pdf: 'PDF',
  csv: 'CSV',
};

function fileKind(file: File, extension: string) {
  if (MARKDOWN_EXTENSIONS.includes(extension)) return 'Markdown';
  if (file.type.startsWith('text/') || extension === 'txt') return 'Text';
  const byExtension = EXTENSION_KIND_LABELS[extension];
  if (byExtension) return byExtension;
  return mimeSubtypeLabel(file.type);
}

function mimeSubtypeLabel(mimeType: string): string {
  return (
    mimeType
      .split('/')
      .pop()
      ?.replace(/[-+].*/, '')
      .toUpperCase() ?? 'File'
  );
}

// Only PubMed is always present; optimistic web-search display would advertise
// a deployment without a provider key.
const FALLBACK_CONNECTORS: Connector[] = [{id: 'pubmed', display: 'PubMed'}];

const WEB_SEARCH_CONNECTOR_ID = 'web_search';

export interface ConnectorToggleProps {
  pubmedEnabled: boolean;
  onPubmedEnabledChange?: (value: boolean) => void;
  webSearchEnabled: boolean;
  onWebSearchEnabledChange?: (value: boolean) => void;
}

function useConnectorsMenu() {
  const sourceControlsRef = useRef<HTMLDivElement>(null);
  const [connectorsOpen, setConnectorsOpen] = useState(false);
  return {connectorsOpen, setConnectorsOpen, sourceControlsRef};
}

interface SourceControlsProps {
  connectorsOpen: boolean;
  onToggleConnectors: () => void;
  sourceControlsRef: RefObject<HTMLDivElement | null>;
  fileInputRef: RefObject<HTMLInputElement | null>;
  onFilesChanged: (e: ChangeEvent<HTMLInputElement>) => void;
  connectors: ConnectorToggleProps;
}

function SourceControls(props: SourceControlsProps) {
  const {
    connectorsOpen,
    onToggleConnectors,
    sourceControlsRef,
    fileInputRef,
    onFilesChanged,
    connectors,
  } = props;
  const connectorsButtonRef = useRef<HTMLButtonElement>(null);
  return (
    <div
      className="reference-composer-source-controls pointer-events-auto relative flex min-w-[4.6rem] items-center gap-2"
      ref={sourceControlsRef}
    >
      <input
        ref={fileInputRef}
        className="reference-file-input absolute size-px overflow-hidden whitespace-nowrap [clip-path:inset(50%)] [clip:rect(0_0_0_0)]"
        type="file"
        multiple
        aria-label="Upload files"
        onChange={onFilesChanged}
        tabIndex={-1}
      />
      <SourceToolbarButton
        label="Files"
        icon="add"
        onClick={() => fileInputRef.current?.click()}
      />
      <SourceToolbarButton
        ref={connectorsButtonRef}
        label="Connectors"
        icon="database"
        expanded={connectorsOpen}
        onClick={onToggleConnectors}
      />
      <ConnectorsMenu
        open={connectorsOpen}
        onClose={onToggleConnectors}
        anchorRefs={[connectorsButtonRef, sourceControlsRef]}
        connectors={connectors}
      />
    </div>
  );
}

// Omit aria-expanded for the non-expandable Files button.
function SourceToolbarButton({
  ref,
  label,
  icon,
  expanded,
  onClick,
}: {
  ref?: RefObject<HTMLButtonElement | null>;
  label: string;
  icon: IconName;
  expanded?: boolean;
  onClick: () => void;
}) {
  return (
    <IconButton
      ref={ref}
      icon={icon}
      label={label}
      layoutClassName="reference-composer-source-button pointer-events-auto"
      aria-expanded={expanded}
      onClick={onClick}
    />
  );
}

function visibleConnectors(status: SystemStatus | null): Connector[] {
  // Unknown availability is different from a genuine empty deployment source
  // list.
  if (status === null) return [];
  return status.connectors?.length ? status.connectors : FALLBACK_CONNECTORS;
}

interface ConnectorBehavior {
  checked: boolean;
  setChecked?: (value: boolean) => void;
  icon: IconName;
}

function literatureBehavior(toggles: ConnectorToggleProps): ConnectorBehavior {
  return {
    checked: toggles.pubmedEnabled,
    setChecked: toggles.onPubmedEnabledChange,
    icon: 'article',
  };
}

// Web search toggles independently; the engine enables the other literature
// connectors as one stack.
const STANDALONE_BEHAVIORS: Record<
  string,
  (toggles: ConnectorToggleProps) => ConnectorBehavior
> = {
  [WEB_SEARCH_CONNECTOR_ID]: toggles => ({
    checked: toggles.webSearchEnabled,
    setChecked: toggles.onWebSearchEnabledChange,
    icon: 'search',
  }),
};

function connectorRowState(
  connector: Connector,
  toggles: ConnectorToggleProps,
): {checked: boolean; toggle: () => void; iconName: IconName} {
  const behavior = (STANDALONE_BEHAVIORS[connector.id] ?? literatureBehavior)(
    toggles,
  );
  return {
    checked: behavior.checked,
    toggle: () => behavior.setChecked?.(!behavior.checked),
    iconName: behavior.icon,
  };
}

function ConnectorMenuRow({
  connector,
  toggles,
}: {
  connector: Connector;
  toggles: ConnectorToggleProps;
}) {
  const {checked, toggle, iconName} = connectorRowState(connector, toggles);
  return (
    <MenuItem
      kind="checkbox"
      checked={checked}
      icon={iconName}
      indicator
      onClick={toggle}
    >
      {connector.display}
    </MenuItem>
  );
}

function ConnectorsNote({text}: {text: string}) {
  return (
    <p
      className="ui-motion-enter m-0 grid min-h-[2.5rem] items-center px-3 text-[0.875rem] text-cosci-muted"
      role="note"
    >
      {text}
    </p>
  );
}

function ConnectorsMenuState({
  status,
  unreachable,
  visible,
}: {
  status: SystemStatus | null;
  unreachable: boolean;
  visible: number;
}) {
  if (visible > 0) return null;
  if (status !== null) {
    return <ConnectorsNote text="No sources are configured for this run." />;
  }
  if (unreachable) {
    return (
      <ConnectorsNote text="Sources unavailable — the API is unreachable." />
    );
  }
  return <ConnectorsNote text="Checking available sources…" />;
}

// Keep checking, unreachable and empty states distinct so fallback sources
// cannot misrepresent deployment availability.
function ConnectorsMenu({
  open,
  onClose,
  anchorRefs,
  connectors: toggles,
}: {
  open: boolean;
  onClose: () => void;
  anchorRefs: RefObject<HTMLElement | null>[];
  connectors: ConnectorToggleProps;
}) {
  const {status, unreachable} = useSystemStatus();
  const connectors = visibleConnectors(status);
  return (
    <Menu
      open={open}
      onClose={onClose}
      label="Connectors"
      anchorRefs={anchorRefs}
      layoutClassName="pointer-events-auto absolute bottom-[2.45rem] left-[2.35rem] z-10 w-56 origin-bottom-left"
    >
      <div className="grid min-h-[2.5rem] items-center border-b border-cosci-menu-divider px-3 text-[0.875rem] font-medium">
        <span>Connectors</span>
      </div>
      <ConnectorsMenuState
        status={status}
        unreachable={unreachable}
        visible={connectors.length}
      />
      {connectors.map(connector => (
        <ConnectorMenuRow
          key={connector.id}
          connector={connector}
          toggles={toggles}
        />
      ))}
    </Menu>
  );
}
