const requests = new Map<string, string>();
export function maintenanceRequestId(instance: string, key: string) {
  const scope = `${instance}:${key}`;
  let id = requests.get(scope);
  if (!id) { id = crypto.randomUUID().replaceAll('-', ''); requests.set(scope, id); }
  return id;
}
export function acknowledgeMaintenanceRequest(instance: string, key: string) { requests.delete(`${instance}:${key}`); }
export function clearMaintenanceRequests() { requests.clear(); }
