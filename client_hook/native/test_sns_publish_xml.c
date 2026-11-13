/* 仅自有数据的 XML 和边界回归，不调用微信。 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "sns_publish_xml.h"

static void rejected(const char *text, size_t length) {
    char output[2048];
    memset(output, '!', sizeof(output));
    assert(!ch_sns_publish_xml(output, sizeof(output), "wxid_self", text, length, 1));
    assert(output[0] == 0 && output[sizeof(output) - 1] == '!');
}

int main(void) {
    char output[4096];
    const char text[] = "&<>'\"\t\r\n\xe4\xb8\xad\xe6\x96\x87";
    size_t count = ch_sns_publish_xml(output, sizeof(output), "wxid_self", text, sizeof(text)-1, UINT32_MAX);
    assert(count == strlen(output));
    assert(strstr(output, "<username>wxid_self</username><createTime>4294967295</createTime>"));
    assert(strstr(output, "<contentDesc>&amp;&lt;&gt;&apos;&quot;\t\r\n\xe4\xb8\xad\xe6\x96\x87</contentDesc>"));
    assert(strstr(output, "<private>0</private>") && strstr(output, "<contentStyle>2</contentStyle>"));
    assert(!strstr(output, "<type>") && !strstr(output, "SnsDataItem"));
    assert(!ch_sns_publish_xml(output, count, "wxid_self", text, sizeof(text)-1, UINT32_MAX));
    assert(output[0] == 0);
    assert(ch_sns_publish_xml(output, count+1, "wxid_self", text, sizeof(text)-1, UINT32_MAX) == count);
    assert(ch_sns_publish_xml(output, sizeof(output), "wxid_self", "ok", 2, 0));
    assert(strstr(output, "<createTime>0</createTime>"));
    assert(!ch_sns_publish_xml(output, sizeof(output), "invalid", "ok", 2, 1));
    assert(!ch_sns_publish_xml(output, sizeof(output), "wxid_x<y", "ok", 2, 1));
    rejected("", 0);
    rejected("x\0y", 3);
    rejected("\1", 1);
    rejected("\xc0\xaf", 2);
    rejected("\xed\xa0\x80", 3);
    rejected("\xf4\x90\x80\x80", 4);
    rejected("\xe2\x82", 2);
    rejected("\xef\xbf\xbe", 3);
    {
        char *long_text = malloc(CH_SNS_PUBLISH_TEXT_MAX + 2u);
        size_t capacity = 6u * CH_SNS_PUBLISH_TEXT_MAX + 2048u;
        char *long_output = malloc(capacity + 16);
        assert(long_text && long_output);
        memset(long_text, '\'', CH_SNS_PUBLISH_TEXT_MAX + 1u);
        long_text[CH_SNS_PUBLISH_TEXT_MAX + 1] = 0;
        memset(long_output, '!', capacity + 16);
        count = ch_sns_publish_xml(long_output, capacity, "wxid_self", long_text, CH_SNS_PUBLISH_TEXT_MAX, 1);
        assert(count > 6u * CH_SNS_PUBLISH_TEXT_MAX && count == strlen(long_output));
        assert(long_output[capacity] == '!');
        assert(!ch_sns_publish_xml(long_output, capacity, "wxid_self", long_text,
                                   CH_SNS_PUBLISH_TEXT_MAX + 1u, 1));
        assert(long_output[0] == 0);
        free(long_text);
        free(long_output);
    }
    puts("SNS publish XML: ok");
    return 0;
}
