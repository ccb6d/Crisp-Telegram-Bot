#!/bin/sh

# conver_to_array(){
#     local BOT_SEND_ID_env=$1
#     local IFS=","
#     str=""
#     for send_id in ${BOT_SEND_ID_env};do
#         str="$str    - ${send_id}\n"
#     done
#     result=`echo -e "${str}"`
# }
AUTOREPLY=`echo -e "${AUTOREPLY}"`
LLM_PAYLOAD=`echo -e "${LLM_PAYLOAD}"`

cat > /Crisp-Telegram-Bot/config.yml << EOF
bot:
  token: ${BOT_TOKEN}
  groupId: ${BOT_GROUPID}
crisp:
  id: ${CRISP_ID}
  key: ${CRISP_KEY}
  website: ${CRISP_WEBSITE}
easyimages:
  apiUrl: ${EasyImages_apiUrl}
  apiToken: ${EasyImages_apiToken}
autoreply:
${AUTOREPLY}
llm:
  enabled: ${LLM_ENABLED:-true}
  apiKey: ${LLM_APIKEY:-dummy}
  baseUrl: ${LLM_BASEURL:-https://api.openai.com/v1}
  model: ${LLM_MODEL:-gpt-4o-mini}
  temperature: ${LLM_TEMPERATURE:-0.4}
  maxTokens: ${LLM_MAXTOKENS:-600}
  payload: |
${LLM_PAYLOAD}
EOF
exec "$@"
