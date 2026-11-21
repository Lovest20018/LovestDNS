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

export function copyTextFromPage(text: string) {
  // The Clipboard API is unavailable on plain HTTP. A selection still allows a
  // user-initiated copy in browsers that support the legacy command.
  return copyText(text, navigator.clipboard ?? {
    async writeText(value) {
      const selected = document.createElement('textarea');
      const previous = document.activeElement;
      selected.value = value;
      selected.readOnly = true;
      selected.style.cssText = 'position:fixed; inset:0 auto auto 0; opacity:0; pointer-events:none';
      document.body.append(selected);
      try {
        selected.select();
        if (!document.execCommand('copy')) throw new Error('Clipboard unavailable');
      } finally {
        selected.remove();
        if (previous instanceof HTMLElement) previous.focus({ preventScroll: true });
      }
    },
  });
}
