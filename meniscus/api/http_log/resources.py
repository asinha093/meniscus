import falcon

from meniscus.api.tenant.resources import MESSAGE_TOKEN
from meniscus.api import (ApiResource, handle_api_exception)
from meniscus.correlation import correlator
from meniscus.api.validator_init import get_validator


class PublishMessageResource(ApiResource):

    @handle_api_exception(operation_name='Publish Message POST')
    @falcon.before(get_validator('correlation'))
    def on_post(self, req, resp, tenant_id, validated_body):
        """
        This method is passed log event data by a tenant. The request will
        have a message token and a tenant id which must be validated either
        by the local cache or by a call to this workers coordinator.
        """

        #Validate the tenant's JSON event log data as valid JSON.
        message = validated_body['log_message']

        #read message token from header
        message_token = req.get_header(MESSAGE_TOKEN, required=True)

        # Queue the message for correlation
        correlator.correlate_http_message.delay(tenant_id,
                                                message_token,
                                                message)

        resp.status = falcon.HTTP_202
