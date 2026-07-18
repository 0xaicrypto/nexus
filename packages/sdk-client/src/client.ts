import { HttpTransport } from './core/http-client.js'
import { TokenStore, localStorageStore } from './core/token-store.js'
import { AuthModule } from './modules/auth.js'
import { ChatModule } from './modules/chat.js'
import { PatientsModule } from './modules/patients.js'
import { ResearchModule } from './modules/research.js'
import { DocumentsModule } from './modules/documents.js'
import { SkillsModule } from './modules/skills.js'
import { SettingsModule } from './modules/settings.js'
import { FilesModule } from './modules/files.js'
import { AdminModule } from './modules/admin.js'
import { MemoryModule } from './modules/memory.js'

export interface HeurionClientOptions {
  baseUrl: string
  tokenStore?: TokenStore
}

export class HeurionClient {
  readonly http: HttpTransport
  readonly auth: AuthModule
  readonly chat: ChatModule
  readonly patients: PatientsModule
  readonly research: ResearchModule
  readonly documents: DocumentsModule
  readonly skills: SkillsModule
  readonly settings: SettingsModule
  readonly files: FilesModule
  readonly admin: AdminModule
  readonly memory: MemoryModule

  constructor(options: HeurionClientOptions) {
    const store = options.tokenStore || localStorageStore
    this.http = new HttpTransport(options.baseUrl, store)
    this.auth = new AuthModule(this.http, store)
    this.chat = new ChatModule(this.http)
    this.patients = new PatientsModule(this.http)
    this.research = new ResearchModule(this.http)
    this.documents = new DocumentsModule(this.http)
    this.skills = new SkillsModule(this.http)
    this.settings = new SettingsModule(this.http)
    this.files = new FilesModule(this.http)
    this.admin = new AdminModule(this.http)
    this.memory = new MemoryModule(this.http)
  }
}
