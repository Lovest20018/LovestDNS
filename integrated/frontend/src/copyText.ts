type ClipboardWriter = Pick<Clipboard, 'writeText'>;

export async function copyText(text: string, clipboard: ClipboardWriter | undefined = globalThis.navigator?.clipboard): Promise<boolean> {
  try {
    if (!clipboard?.writeText) return false;
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
