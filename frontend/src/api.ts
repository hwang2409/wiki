import type { Note, NoteDraft, NoteSummary } from "./types";

function encodeNotePath(path: string) {
  return path.split("/").map(encodeURIComponent).join("/");
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {})
    }
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = body?.detail ?? `Request failed with ${response.status}`;
    throw new Error(message);
  }

  return response.json() as Promise<T>;
}

export function listNotes() {
  return request<NoteSummary[]>("/api/notes");
}

export function searchNotes(query: string) {
  return request<NoteSummary[]>(`/api/notes?q=${encodeURIComponent(query)}`);
}

export function getNote(path: string) {
  return request<Note>(`/api/notes/${encodeNotePath(path)}`);
}

export function createNote(draft: NoteDraft) {
  return request<Note>("/api/notes", {
    method: "POST",
    body: JSON.stringify({
      title: draft.title,
      path: draft.path || null,
      content: draft.content
    })
  });
}

export function updateNote(path: string, content: string) {
  return request<Note>(`/api/notes/${encodeNotePath(path)}`, {
    method: "PUT",
    body: JSON.stringify({ content })
  });
}

