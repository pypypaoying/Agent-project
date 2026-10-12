export type RunStatus = 'running' | 'awaiting_input' | 'cancelling' | 'completed' | 'error' | 'cancelled' | 'interrupted';
export interface Conversation { id: string; title: string; frozen?: boolean; inputs_frozen?: boolean; updated_at?: string; }
export interface TablePreview { columns: unknown[]; rows: unknown[][]; row_count: number; offset?: number; }
export interface ManagedFile {
  id: string; name: string; role?: 'csv' | 'material'; size?: number; media_type?: string;
  preview?: Partial<TablePreview> & { text?: string }; parse_error?: unknown;
}
export interface Artifact {
  id: string; kind: string; label?: string; row_count?: number; columns?: unknown[]; value?: unknown;
  files: ManagedFile[];
}
export interface PendingInput { id: string; kind: string; question: string; candidates?: unknown[]; }
export interface RunEvent {
  version: number; conversation_id: string; run_id: string; seq: number; type: string; time?: string;
  payload: Record<string, unknown>;
}
export interface Run {
  id: string; conversation_id?: string; task?: string; status: RunStatus; final_answer?: unknown;
  error?: unknown; pending_input?: PendingInput | null; artifacts?: Artifact[]; downloads?: ManagedFile[];
  events?: RunEvent[]; created_at?: string; context_run_ids?: string[];
  model_selection?: ModelSelection & { provider_name?: string; base_url?: string };
}
export interface Message { id: string; role: string; content: string; state?: string; run_id?: string; question_id?: string; }
export interface ConversationDetail { conversation: Conversation; files: ManagedFile[]; messages: Message[]; runs: Run[]; model_selection?: ModelSelection; }
export interface ModelSelection { provider_id: string; model_id: string; }
export interface Provider {
  id: string; name: string; kind: 'preset' | 'custom' | 'env'; base_url: string;
  models: string[]; default_model_id: string; has_key: boolean; key_source?: string; revision: number;
}
export interface ProviderCatalog {
  providers: Provider[]; default_provider_id: string; default_model_id: string;
  credential_persistence?: string;
}
export interface ProviderUpdate {
  name: string; base_url: string; models: string[]; default_model_id: string;
  api_key?: string; clear_key?: boolean;
}
export interface ProviderKeyUpdate { api_key?: string; clear_key?: boolean; }
