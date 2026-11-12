import type { Installation } from './suiteApi.ts';

export function nativeMaintenanceCommands(installation?: Installation): string | null {
  const ctl = installation?.control_command;
  if (installation?.source !== 'native_service_environment' || !ctl
    || !/^\/usr\/local\/sbin\/lovestdns(?:-[a-z0-9][a-z0-9-]{0,30})?ctl$/.test(ctl)) return null;
  return `sudo ${ctl} status\nsudo ${ctl} health\nsudo ${ctl} restart\n# 升级时使用已验证的安装包与 SHA-256\nsudo ${ctl} upgrade /path/to/lovestdns.tar.gz --sha256 <SHA-256>`;
}
