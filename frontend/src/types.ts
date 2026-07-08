export interface NoteSummary {
  id: string;
  path: string;
  title: string;
  excerpt: string;
  updated_at: string;
  note_type?: string | null;
  meta_updated?: string | null;
}

export interface Note extends NoteSummary {
  content: string;
}

export interface NoteDraft {
  title: string;
  path: string;
  content: string;
}

