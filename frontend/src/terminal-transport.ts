export type TerminalInputFrame = string | Uint8Array;

export function terminalInputFrame(
  data: string,
  binaryInputSupported: boolean,
  encoder: TextEncoder = new TextEncoder()
): TerminalInputFrame {
  return binaryInputSupported
    ? encoder.encode(data)
    : JSON.stringify({ type: "input", data });
}
