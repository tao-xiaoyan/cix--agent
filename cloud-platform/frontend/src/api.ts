// The event union is generated directly from the frozen shared JSON Schema.
import type { Event as EventSchema } from './events'
export type Event = EventSchema
export interface Page<T> {items:T[];next_cursor:string|null}
export interface Device {device_id:string;last_seen:string|null;connectivity:string}
export interface Task {task_id:string;device_id:string;status:string;created_at:string;updated_at:string;result_event_id:string|null;error:{message:string}|null}
export interface Dashboard {event_count:number;anomaly_count:number;device_count:number;latest_sync:string|null;event_types:Record<string,number>;devices:{device_id:string;event_count:number;last_sync:string|null}[]}
export async function api<T>(path:string, token:string, init:RequestInit={}):Promise<T> {
 const response = await fetch(path,{...init,headers:{'Content-Type':'application/json','Authorization':`Bearer ${token}`,...init.headers}})
 const body = await response.json()
 if(!response.ok) throw new Error(`${body.error?.message ?? 'API 请求失败'} (${response.status}) · ${body.request_id ?? ''}`)
 return body
}

