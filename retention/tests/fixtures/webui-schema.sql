CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
CREATE TABLE auth (
	id VARCHAR NOT NULL, 
	email VARCHAR, 
	password TEXT, 
	active BOOLEAN, mfa JSON, session_stamp TEXT, 
	PRIMARY KEY (id)
);
CREATE TABLE chat (
	id VARCHAR NOT NULL, 
	user_id VARCHAR, 
	title TEXT, 
	created_at BIGINT, 
	updated_at BIGINT, 
	share_id TEXT, 
	archived BOOLEAN, chat JSON, pinned BOOLEAN, meta JSON DEFAULT '{}' NOT NULL, folder_id TEXT, tasks JSON, summary TEXT, last_read_at BIGINT, current_message_id TEXT, variables JSON, timer_at BIGINT, 
	PRIMARY KEY (id), 
	UNIQUE (share_id)
);
CREATE TABLE chatidtag (
	id VARCHAR NOT NULL, 
	tag_name VARCHAR, 
	chat_id VARCHAR, 
	user_id VARCHAR, 
	timestamp BIGINT, 
	PRIMARY KEY (id)
);
CREATE TABLE document (
	collection_name VARCHAR NOT NULL, 
	name VARCHAR, 
	title TEXT, 
	filename TEXT, 
	content TEXT, 
	user_id VARCHAR, 
	timestamp BIGINT, 
	PRIMARY KEY (collection_name), 
	UNIQUE (name)
);
CREATE TABLE function (
	id VARCHAR NOT NULL, 
	user_id VARCHAR, 
	name TEXT, 
	type TEXT, 
	content TEXT, 
	meta TEXT, 
	valves TEXT, 
	is_active BOOLEAN, 
	is_global BOOLEAN, 
	updated_at BIGINT, 
	created_at BIGINT, version_id TEXT, 
	PRIMARY KEY (id)
);
CREATE TABLE memory (
	id VARCHAR NOT NULL, 
	user_id VARCHAR, 
	content TEXT, 
	updated_at BIGINT, 
	created_at BIGINT, type VARCHAR DEFAULT 'context' NOT NULL, path TEXT, meta JSON, 
	PRIMARY KEY (id)
);
CREATE TABLE "config_old" (
	id INTEGER NOT NULL, 
	data JSON NOT NULL, 
	version INTEGER NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, 
	PRIMARY KEY (id)
);
CREATE TABLE "tag" (
	id VARCHAR NOT NULL, 
	name VARCHAR, 
	user_id VARCHAR, 
	meta JSON, 
	CONSTRAINT pk_id_user_id PRIMARY KEY (id, user_id)
);
CREATE TABLE feedback (
	id TEXT NOT NULL, 
	user_id TEXT, 
	version BIGINT, 
	type TEXT, 
	data JSON, 
	meta JSON, 
	snapshot JSON, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE "folder" (
	id TEXT NOT NULL, 
	parent_id TEXT, 
	user_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	items JSON, 
	meta JSON, 
	is_expanded BOOLEAN NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, data JSON, 
	PRIMARY KEY (id, user_id)
);
CREATE TABLE message (
	id TEXT NOT NULL, 
	user_id TEXT, 
	channel_id TEXT, 
	content TEXT, 
	data JSON, 
	meta JSON, 
	created_at BIGINT, 
	updated_at BIGINT, parent_id TEXT, reply_to_id TEXT, is_pinned BOOLEAN DEFAULT 0 NOT NULL, pinned_at BIGINT, pinned_by TEXT, app_id TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (id)
);
CREATE TABLE message_reaction (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	message_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	created_at BIGINT, 
	PRIMARY KEY (id), 
	UNIQUE (id)
);
CREATE INDEX folder_id_idx ON chat (folder_id);
CREATE INDEX user_id_pinned_idx ON chat (user_id, pinned);
CREATE INDEX user_id_archived_idx ON chat (user_id, archived);
CREATE INDEX updated_at_user_id_idx ON chat (updated_at, user_id);
CREATE INDEX folder_id_user_id_idx ON chat (folder_id, user_id);
CREATE INDEX user_id_idx ON tag (user_id);
CREATE INDEX is_global_idx ON function (is_global);
CREATE TABLE oauth_session (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	provider TEXT NOT NULL, 
	token TEXT NOT NULL, 
	expires_at BIGINT NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE INDEX idx_oauth_session_user_id ON oauth_session (user_id);
CREATE INDEX idx_oauth_session_expires_at ON oauth_session (expires_at);
CREATE INDEX idx_oauth_session_user_provider ON oauth_session (user_id, provider);
CREATE TABLE group_member (
	id TEXT NOT NULL, 
	group_id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	created_at BIGINT, 
	updated_at BIGINT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_group_member_group_user UNIQUE (group_id, user_id), 
	UNIQUE (id), 
	FOREIGN KEY(group_id) REFERENCES "group" (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE api_key (
	id TEXT NOT NULL, 
	user_id TEXT, 
	"key" TEXT NOT NULL, 
	data JSON, 
	expires_at BIGINT, 
	last_used_at BIGINT, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE, 
	UNIQUE ("key")
);
CREATE TABLE "user" (
	id VARCHAR NOT NULL, 
	name VARCHAR, 
	email VARCHAR, 
	role VARCHAR, 
	profile_image_url TEXT, 
	last_active_at BIGINT, 
	updated_at BIGINT, 
	created_at BIGINT, 
	username VARCHAR(50), 
	bio TEXT, 
	gender TEXT, 
	date_of_birth DATE, 
	profile_banner_image_url TEXT, 
	timezone VARCHAR, 
	presence_state VARCHAR, 
	status_emoji VARCHAR, 
	status_message TEXT, 
	status_expires_at BIGINT, 
	oauth JSON, 
	info JSON, 
	settings JSON, scim JSON, variables JSON, 
	PRIMARY KEY (id)
);
CREATE TABLE channel_webhook (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	channel_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	profile_image_url TEXT, 
	token TEXT NOT NULL, 
	last_used_at BIGINT, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (id), 
	FOREIGN KEY(channel_id) REFERENCES channel (id) ON DELETE CASCADE
);
CREATE TABLE "channel_file" (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	channel_id TEXT NOT NULL, 
	file_id TEXT NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	message_id TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_channel_file_channel_file UNIQUE (channel_id, file_id), 
	CONSTRAINT fk_channel_file_message_id FOREIGN KEY(message_id) REFERENCES message (id) ON DELETE CASCADE, 
	FOREIGN KEY(file_id) REFERENCES file (id) ON DELETE CASCADE, 
	FOREIGN KEY(channel_id) REFERENCES channel (id) ON DELETE CASCADE
);
CREATE INDEX ix_channel_file_file_id ON channel_file (file_id);
CREATE INDEX ix_channel_file_channel_id ON channel_file (channel_id);
CREATE INDEX ix_channel_file_user_id ON channel_file (user_id);
CREATE TABLE chat_file (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	chat_id TEXT NOT NULL, 
	file_id TEXT NOT NULL, 
	message_id TEXT, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_file_chat_file UNIQUE (chat_id, file_id), 
	FOREIGN KEY(chat_id) REFERENCES chat (id) ON DELETE CASCADE, 
	FOREIGN KEY(file_id) REFERENCES file (id) ON DELETE CASCADE
);
CREATE INDEX ix_chat_file_user_id ON chat_file (user_id);
CREATE INDEX ix_chat_file_file_id ON chat_file (file_id);
CREATE INDEX ix_chat_file_chat_id ON chat_file (chat_id);
CREATE INDEX ix_chat_file_message_id ON chat_file (message_id);
CREATE TABLE prompt_history (
	id TEXT NOT NULL, 
	prompt_id TEXT NOT NULL, 
	parent_id TEXT, 
	snapshot JSON NOT NULL, 
	user_id TEXT NOT NULL, 
	commit_message TEXT, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_prompt_history_prompt_id ON prompt_history (prompt_id);
CREATE TABLE chat_message (
	id TEXT NOT NULL, 
	chat_id TEXT NOT NULL, 
	user_id TEXT, 
	role TEXT NOT NULL, 
	parent_id TEXT, 
	content JSON, 
	output JSON, 
	model_id TEXT, 
	files JSON, 
	sources JSON, 
	embeds JSON, 
	done BOOLEAN, 
	status_history JSON, 
	error JSON, 
	usage JSON, 
	created_at BIGINT, 
	updated_at BIGINT, context_summary TEXT, meta JSON, 
	PRIMARY KEY (id), 
	FOREIGN KEY(chat_id) REFERENCES chat (id) ON DELETE CASCADE
);
CREATE INDEX ix_chat_message_created_at ON chat_message (created_at);
CREATE INDEX ix_chat_message_user_id ON chat_message (user_id);
CREATE INDEX ix_chat_message_model_id ON chat_message (model_id);
CREATE INDEX ix_chat_message_chat_id ON chat_message (chat_id);
CREATE INDEX chat_message_chat_parent_idx ON chat_message (chat_id, parent_id);
CREATE INDEX chat_message_model_created_idx ON chat_message (model_id, created_at);
CREATE INDEX chat_message_user_created_idx ON chat_message (user_id, created_at);
CREATE TABLE access_grant (
	id TEXT NOT NULL, 
	resource_type TEXT NOT NULL, 
	resource_id TEXT NOT NULL, 
	principal_type TEXT NOT NULL, 
	principal_id TEXT NOT NULL, 
	permission TEXT NOT NULL, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_access_grant_grant UNIQUE (resource_type, resource_id, principal_type, principal_id, permission)
);
CREATE INDEX idx_access_grant_resource ON access_grant (resource_type, resource_id);
CREATE INDEX idx_access_grant_principal ON access_grant (principal_type, principal_id);
CREATE TABLE "knowledge" (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	description TEXT, 
	meta JSON, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT, 
	data JSON, 
	PRIMARY KEY (id)
);
CREATE TABLE "prompt" (
	id TEXT NOT NULL, 
	command VARCHAR, 
	user_id VARCHAR NOT NULL, 
	name TEXT NOT NULL, 
	content TEXT NOT NULL, 
	data JSON, 
	meta JSON, 
	is_active BOOLEAN DEFAULT '1' NOT NULL, 
	version_id TEXT, 
	tags JSON, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_prompt_new_command ON prompt (command);
CREATE TABLE "tool" (
	id VARCHAR NOT NULL, 
	user_id VARCHAR, 
	name TEXT, 
	content TEXT, 
	specs TEXT, 
	meta TEXT, 
	valves TEXT, 
	updated_at BIGINT, 
	created_at BIGINT, version_id TEXT, is_active BOOLEAN DEFAULT 1 NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE "model" (
	id TEXT NOT NULL, 
	user_id TEXT, 
	base_model_id TEXT, 
	name TEXT, 
	params TEXT, 
	meta TEXT, 
	updated_at BIGINT, 
	created_at BIGINT, 
	is_active BOOLEAN DEFAULT 1 NOT NULL, version_id TEXT, 
	PRIMARY KEY (id)
);
CREATE TABLE "channel" (
	id TEXT NOT NULL, 
	user_id TEXT, 
	name TEXT, 
	description TEXT, 
	data JSON, 
	meta JSON, 
	created_at BIGINT, 
	updated_at BIGINT, 
	type TEXT, 
	is_private BOOLEAN, 
	archived_at BIGINT, 
	archived_by TEXT, 
	deleted_at BIGINT, 
	deleted_by TEXT, 
	updated_by TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (id)
);
CREATE TABLE "file" (
	id VARCHAR NOT NULL, 
	user_id VARCHAR, 
	filename TEXT, 
	meta JSON, 
	created_at BIGINT, 
	hash TEXT, 
	data JSON, 
	updated_at BIGINT, 
	path TEXT, 
	PRIMARY KEY (id)
);
CREATE TABLE skill (
	id VARCHAR NOT NULL, 
	user_id VARCHAR NOT NULL, 
	name TEXT NOT NULL, 
	description TEXT, 
	content TEXT NOT NULL, 
	meta JSON, 
	is_active BOOLEAN NOT NULL, 
	updated_at BIGINT NOT NULL, 
	created_at BIGINT NOT NULL, version_id TEXT, data JSON, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE INDEX idx_skill_user_id ON skill (user_id);
CREATE INDEX idx_skill_updated_at ON skill (updated_at);
CREATE TABLE automation (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	data JSON NOT NULL, 
	meta JSON, 
	is_active BOOLEAN NOT NULL, 
	last_run_at BIGINT, 
	next_run_at BIGINT, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, folder_id TEXT, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_automation_next_run ON automation (next_run_at);
CREATE TABLE automation_run (
	id TEXT NOT NULL, 
	automation_id TEXT NOT NULL, 
	chat_id TEXT, 
	status TEXT NOT NULL, 
	error TEXT, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_automation_run_automation_id ON automation_run (automation_id);
CREATE TABLE shared_chat (
	id TEXT NOT NULL, 
	chat_id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	title TEXT, 
	chat JSON, 
	created_at BIGINT, 
	updated_at BIGINT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(chat_id) REFERENCES chat (id) ON DELETE CASCADE
);
CREATE TABLE calendar (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	color TEXT, 
	is_default BOOLEAN NOT NULL, 
	data JSON, 
	meta JSON, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_calendar_user ON calendar (user_id);
CREATE TABLE calendar_event (
	id TEXT NOT NULL, 
	calendar_id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	title TEXT NOT NULL, 
	description TEXT, 
	start_at BIGINT NOT NULL, 
	end_at BIGINT, 
	all_day BOOLEAN NOT NULL, 
	rrule TEXT, 
	color TEXT, 
	location TEXT, 
	data JSON, 
	meta JSON, 
	is_cancelled BOOLEAN NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_calendar_event_calendar ON calendar_event (calendar_id, start_at);
CREATE INDEX ix_calendar_event_user_date ON calendar_event (user_id, start_at);
CREATE TABLE calendar_event_attendee (
	id TEXT NOT NULL, 
	event_id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	status TEXT NOT NULL, 
	meta JSON, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_event_attendee UNIQUE (event_id, user_id)
);
CREATE INDEX ix_calendar_event_attendee_user ON calendar_event_attendee (user_id, status);
CREATE TABLE pinned_note (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	note_id TEXT NOT NULL, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pinned_note UNIQUE (user_id, note_id), 
	FOREIGN KEY(note_id) REFERENCES note (id) ON DELETE CASCADE
);
CREATE TABLE "note" (
	id TEXT NOT NULL, 
	user_id TEXT, 
	title TEXT, 
	data JSON, 
	meta JSON, 
	created_at BIGINT, 
	updated_at BIGINT, 
	PRIMARY KEY (id), 
	UNIQUE (id)
);
CREATE INDEX ix_memory_user_id ON memory (user_id);
CREATE TABLE knowledge_directory (
	id TEXT NOT NULL, 
	knowledge_id TEXT NOT NULL, 
	parent_id TEXT, 
	name TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(knowledge_id) REFERENCES knowledge (id) ON DELETE CASCADE, 
	FOREIGN KEY(parent_id) REFERENCES knowledge_directory (id) ON DELETE CASCADE, 
	CONSTRAINT uq_knowledge_directory_knowledge_parent_name UNIQUE (knowledge_id, parent_id, name)
);
CREATE INDEX ix_knowledge_directory_knowledge_id ON knowledge_directory (knowledge_id);
CREATE INDEX ix_knowledge_directory_parent_id ON knowledge_directory (parent_id);
CREATE TABLE "knowledge_file" (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	knowledge_id TEXT NOT NULL, 
	file_id TEXT NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	directory_id TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_knowledge_file_knowledge_file UNIQUE (knowledge_id, file_id), 
	CONSTRAINT fk_knowledge_file_directory_id FOREIGN KEY(directory_id) REFERENCES knowledge_directory (id) ON DELETE SET NULL, 
	FOREIGN KEY(file_id) REFERENCES file (id) ON DELETE CASCADE, 
	FOREIGN KEY(knowledge_id) REFERENCES knowledge (id) ON DELETE CASCADE
);
CREATE INDEX ix_knowledge_file_user_id ON knowledge_file (user_id);
CREATE INDEX ix_knowledge_file_file_id ON knowledge_file (file_id);
CREATE INDEX ix_knowledge_file_knowledge_id ON knowledge_file (knowledge_id);
CREATE INDEX ix_knowledge_file_directory_id ON knowledge_file (directory_id);
CREATE TABLE config (
	"key" TEXT NOT NULL, 
	value JSON NOT NULL, 
	updated_at BIGINT, 
	PRIMARY KEY ("key")
);
CREATE INDEX ix_memory_type ON memory (type);
CREATE INDEX ix_memory_id_user_id ON memory (id, user_id);
CREATE INDEX ix_automation_user_folder ON automation (user_id, folder_id);
CREATE UNIQUE INDEX uq_user_email_lower ON user (lower(email)) WHERE email IS NOT NULL;
CREATE INDEX ix_group_member_user_id_group_id ON group_member (user_id, group_id);
CREATE INDEX timer_at_idx ON chat (timer_at) WHERE timer_at IS NOT NULL;
CREATE INDEX user_id_updated_at_id_idx ON chat (user_id, updated_at DESC, id);
CREATE INDEX user_id_timer_at_idx ON chat (user_id, timer_at) WHERE timer_at IS NOT NULL;
CREATE INDEX user_id_folder_unread_idx ON chat (user_id, folder_id, archived, updated_at, last_read_at, id);
CREATE INDEX chat_message_chat_role_done_idx ON chat_message (chat_id, role, done);
CREATE TABLE "group" (
	id TEXT NOT NULL, 
	user_id TEXT, 
	name TEXT, 
	description TEXT, 
	data JSON, 
	meta JSON, 
	permissions JSON, 
	created_at BIGINT, 
	updated_at BIGINT, 
	parent_group_id TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_group_parent FOREIGN KEY(parent_group_id) REFERENCES "group" (id) ON DELETE SET NULL, 
	UNIQUE (id)
);
CREATE INDEX ix_group_parent_group_id ON "group" (parent_group_id);
CREATE TABLE skill_history (
	id TEXT NOT NULL, 
	skill_id TEXT NOT NULL, 
	parent_id TEXT, 
	snapshot JSON NOT NULL, 
	user_id TEXT NOT NULL, 
	commit_message TEXT, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_skill_history_skill_id ON skill_history (skill_id);
CREATE TABLE model_history (
	id TEXT NOT NULL, 
	model_id TEXT NOT NULL, 
	parent_id TEXT, 
	snapshot JSON NOT NULL, 
	user_id TEXT NOT NULL, 
	commit_message TEXT, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_model_history_model_id ON model_history (model_id);
CREATE TABLE tool_history (
	id TEXT NOT NULL, 
	tool_id TEXT NOT NULL, 
	parent_id TEXT, 
	snapshot JSON NOT NULL, 
	user_id TEXT NOT NULL, 
	commit_message TEXT, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_tool_history_tool_id ON tool_history (tool_id);
CREATE TABLE function_history (
	id TEXT NOT NULL, 
	function_id TEXT NOT NULL, 
	parent_id TEXT, 
	snapshot JSON NOT NULL, 
	user_id TEXT NOT NULL, 
	commit_message TEXT, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_function_history_function_id ON function_history (function_id);
CREATE TABLE app (
	id TEXT NOT NULL, 
	slug TEXT NOT NULL, 
	name TEXT NOT NULL, 
	user_id TEXT, 
	is_active BOOLEAN DEFAULT 0 NOT NULL, 
	data JSON NOT NULL, 
	api_key TEXT, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
);
CREATE TABLE "channel_member" (
	id TEXT NOT NULL, 
	channel_id TEXT NOT NULL, 
	user_id TEXT, 
	created_at BIGINT, 
	status TEXT, 
	is_active BOOLEAN DEFAULT 1 NOT NULL, 
	is_channel_muted BOOLEAN DEFAULT 0 NOT NULL, 
	is_channel_pinned BOOLEAN DEFAULT 0 NOT NULL, 
	data JSON, 
	meta JSON, 
	joined_at BIGINT NOT NULL, 
	left_at BIGINT, 
	last_read_at BIGINT, 
	updated_at BIGINT, 
	role TEXT, 
	invited_by TEXT, 
	invited_at BIGINT, 
	app_id TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_channel_member_identity CHECK ((user_id IS NULL) <> (app_id IS NULL)), 
	CONSTRAINT uq_channel_member_app UNIQUE (channel_id, app_id), 
	UNIQUE (id)
);
CREATE INDEX ix_channel_member_app_active ON channel_member (app_id, is_active);
CREATE INDEX ix_message_app ON message (app_id);
CREATE TABLE comment (
	id TEXT NOT NULL, 
	user_id TEXT, 
	resource_type TEXT NOT NULL, 
	resource_id TEXT NOT NULL, 
	parent_id TEXT, 
	reply_to_id TEXT, 
	content TEXT NOT NULL, 
	target_type TEXT, 
	target_id TEXT, 
	meta JSON, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	deleted_at BIGINT, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_comment_resource ON comment (resource_type, resource_id, parent_id, created_at, id);
CREATE INDEX ix_comment_parent ON comment (parent_id, created_at, id);
CREATE INDEX ix_comment_target ON comment (resource_type, resource_id, target_type, target_id);
CREATE TABLE comment_reaction (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	comment_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	created_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_comment_reaction UNIQUE (comment_id, user_id, name)
);
CREATE INDEX ix_comment_reaction_comment_id ON comment_reaction (comment_id);
CREATE TABLE notification (
	id TEXT NOT NULL, 
	user_id TEXT NOT NULL, 
	event_type TEXT NOT NULL, 
	event_key TEXT NOT NULL, 
	actor_type TEXT NOT NULL, 
	actor_id TEXT, 
	resource_type TEXT, 
	resource_id TEXT, 
	object_type TEXT, 
	object_id TEXT, 
	data JSON NOT NULL, 
	seen_at BIGINT, 
	read_at BIGINT, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_notification_occurrence UNIQUE (user_id, event_key), 
	CONSTRAINT ck_notification_resource CHECK ((resource_type IS NULL) = (resource_id IS NULL)), 
	CONSTRAINT ck_notification_object CHECK ((object_type IS NULL) = (object_id IS NULL)), 
	CONSTRAINT ck_notification_actor CHECK (actor_type IN ('user', 'system'))
);
CREATE INDEX ix_notification_user_created ON notification (user_id, created_at, id);
CREATE INDEX ix_notification_unseen ON notification (user_id, created_at, id) WHERE seen_at IS NULL;
CREATE INDEX ix_notification_resource ON notification (resource_type, resource_id);
CREATE INDEX ix_notification_object ON notification (object_type, object_id);
CREATE TABLE auth_session (
	id TEXT NOT NULL, 
	user_id TEXT, 
	refresh_token_id TEXT, 
	data JSON NOT NULL, 
	created_at BIGINT NOT NULL, 
	updated_at BIGINT NOT NULL, 
	expires_at BIGINT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX idx_auth_session_user ON auth_session (user_id);
CREATE INDEX idx_auth_session_expires ON auth_session (expires_at);
CREATE TABLE chat_view_state (
	user_id TEXT NOT NULL, 
	chat_id TEXT NOT NULL, 
	state JSON NOT NULL, 
	updated_at BIGINT NOT NULL, 
	PRIMARY KEY (user_id, chat_id)
);
CREATE INDEX ix_chat_view_state_chat_id ON chat_view_state (chat_id);
CREATE INDEX ix_message_channel_parent_created ON message (channel_id, parent_id, created_at);
CREATE INDEX ix_message_parent_id ON message (parent_id);
CREATE INDEX ix_message_reaction_message_id ON message_reaction (message_id);
