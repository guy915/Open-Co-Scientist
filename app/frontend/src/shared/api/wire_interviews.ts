// Generated from co_scientist.api.contracts; edit the backend models.

export interface ChatSummary {
  id: string;
  title: string | null;
  challenge: string;
  status: 'active' | 'completed' | 'cancelled';
  run_id: string | null;
  created_at: number;
  updated_at: number;
}

export interface Interview {
  id: string;
  client_id: string;
  status: 'active' | 'completed' | 'cancelled';
  fields: InterviewFields;
  current_question: string | null;
  turns: InterviewTurn[];
  documents?: InterviewDocument[];
  created_at: number;
  updated_at: number;
  completed_at: number | null;
  run_id?: string | null;
}

export interface InterviewDocument {
  id: string;
  title: string;
  mime_type: string;
  byte_size: number;
}

export interface InterviewFields {
  research_challenge: string;
  focus_area: string[];
  preferences: string[];
  lab_constraints: string[];
  title: string | null;
}

export interface InterviewQuestion {
  header: string;
  question: string;
  multi_select: boolean;
  options: InterviewQuestionOption[];
}

export interface InterviewQuestionOption {
  label: string;
  description: string;
}

export interface InterviewTurn {
  id: number;
  role: 'user' | 'agent';
  content: string;
  reasoning: string | null;
  fallback: boolean;
  questions: InterviewQuestion[];
  created_at: number;
}
