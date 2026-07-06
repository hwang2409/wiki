export interface NoteSummary {
  id: string;
  path: string;
  title: string;
  excerpt: string;
  updated_at: string;
}

export interface Note extends NoteSummary {
  content: string;
}

export interface NoteDraft {
  title: string;
  path: string;
  content: string;
}

