import { useStore } from './store';
import { ENV_VARS } from './types';
import YAML from 'yaml';

function isDefault(key: string, value: string): boolean {
  const envVar = ENV_VARS.find((v) => v.key === key);
  return envVar ? envVar.defaultValue === value : false;
}

export function generateDockerRun(dataPath: string = '/home/mydata'): string {
  const { envValues } = useStore.getState();
  const containerName = 'paopaodns';

  const parts: string[] = ['docker run -d'];
  parts.push(`--name ${containerName}`);
  parts.push(`-v ${dataPath}:/data`);

  const envKeys = Object.keys(envValues).filter((k) => !isDefault(k, envValues[k]));
  for (const key of envKeys) {
    parts.push(`-e ${key}=${envValues[key]}`);
  }

  parts.push('--restart always');
  parts.push('-p 53:53/udp -p 53:53/tcp');
  parts.push('sliamb/paopaodns');
  return parts.join(' \\\n');
}

export function generateDockerCompose(dataPath: string = '/home/mydata'): string {
  const { envValues } = useStore.getState();
  const containerName = 'paopaodns';

  const envEntries: Record<string, string> = {};
  Object.keys(envValues).forEach((k) => {
    if (!isDefault(k, envValues[k])) {
      envEntries[k] = envValues[k];
    }
  });

  const lines: string[] = [
    'services:',
    `  paopaodns:`,
    `    image: sliamb/paopaodns:latest`,
    `    container_name: ${containerName}`,
    `    restart: always`,
    `    volumes:`,
    `      - ${dataPath}:/data`,
    `    environment:`,
  ];

  Object.entries(envEntries).forEach(([k, v]) => {
    lines.push(`      - ${k}=${v}`);
  });

  lines.push('    ports:');
  lines.push('      - "53:53/udp"');
  lines.push('      - "53:53/tcp"');

  return lines.join('\n');
}

export function generateCustomEnvIni(): string {
  const { customEnvEntries } = useStore.getState();
  const lines: string[] = [
    '# Variables configured here',
    '# override the ENV at docker startup.',
    '# MosDNS reload if Modifying this file.',
    '# Format: key="value"',
    '',
  ];
  customEnvEntries.forEach((e) => {
    if (e.enabled && e.key) {
      lines.push(`${e.key}="${e.value}"`);
    } else if (e.key) {
      lines.push(`#${e.key}="${e.value}"`);
    }
  });
  return lines.join('\n');
}

export function generateCustomModYaml(): string {
  const { customModZones, customModSwaps, customModHosts } = useStore.getState();

  // Build a plain object and let the YAML serializer handle quoting/escaping,
  // mirroring DeployPage's YAML.stringify approach. Manual string building would
  // emit broken YAML for values containing ':', quotes, or leading indicators.
  const doc: Record<string, unknown> = {};

  if (customModZones.length > 0) {
    doc.Zones = customModZones.map((z) => ({
      zone: z.zone,
      dns: z.dns,
      ttl: z.ttl,
      seq: z.seq,
      socks5: z.socks5,
    }));
  }

  if (customModSwaps.length > 0) {
    doc.Swaps = customModSwaps.map((s) => ({
      env_key: s.env_key,
      cidr_file: s.cidr_file,
    }));
  }

  if (customModHosts.length > 0) {
    doc.Hosts = customModHosts.map((h) => ({
      env_key: h.env_key,
      zone: h.zone,
    }));
  }

  if (Object.keys(doc).length === 0) {
    return `# Custom Mod Configuration\n# Add Zones, Swaps, or Hosts configurations below\n`;
  }
  return YAML.stringify(doc);
}

export function downloadFile(content: string, filename: string) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function copyToClipboard(text: string): Promise<void> {
  return navigator.clipboard.writeText(text);
}
