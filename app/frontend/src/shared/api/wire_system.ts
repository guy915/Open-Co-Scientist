// Generated from co_scientist.api.contracts; edit the backend models.

export interface ClientLogRecord {
  message: string;
  level?: string;
  logger?: string;
  run_id?: string | null;
}

export interface Connector {
  id: string;
  display: string;
}

export interface FeedbackRequest {
  category:
    'Bug' | 'Security' | 'Results quality' | 'Feature request' | 'Other';
  message: string;
  diagnostics: string;
  url: string;
  run_id?: string | null;
}

export interface ProbeStatus {
  state: string;
  error: string | null;
}

export interface SystemStatusResponse {
  mcp_available: boolean;
  pubmed_available: boolean;
  literature_review_available: boolean;
  web_search_available: boolean;
  email_notifications_available: boolean;
  probes: Record<string, ProbeStatus> | null;
  mcp_server_url: string | null;
  provider: string;
  llm_backend: string;
  has_provider_key: boolean | null;
  byok_enabled: boolean | null;
  engine_importable: boolean | null;
  model_name: string;
  supervisor_model_name: string | null;
  enabled_tools: string[] | null;
  connectors: Connector[];
}
