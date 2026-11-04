// Preserve comments, unknown keys and line endings while editing named overrides.
export function envOverrides(content: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of content.split(/\r?\n/)) {
    const match = line.match(/^([_a-zA-Z0-9]+)="([^"\r\n]*)"$/);
    if (match) result[match[1]] = match[2];
  }
  return result;
}
export function editOverride(content: string, key: string, value: string | null) {
  const newline = content.includes('\r\n') ? '\r\n' : '\n';
  const lines = content.split(/\r?\n/);
  const assignment = `${key}="${value ?? ''}"`;
  let found = false;
  const next = lines.flatMap((line) => {
    if (!line.startsWith(`${key}=`)) return [line];
    if (found) return [`# ${line}`];
    found = true;
    return [value === null ? `# ${line}` : assignment];
  });
  if (!found && value !== null) {
    if (next.at(-1) === '') next.splice(-1, 0, assignment);
    else next.push(assignment, '');
  }
  return next.join(newline);
}
