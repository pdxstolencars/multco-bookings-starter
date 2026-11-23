from full_record_parser import parse_full_record_html


SAMPLE = '''<html><input id="BookingNumber" value="123456" />
<div id="booking-detail"><table>
<tr><td><label>Name</label></td><td>Example, Test Person</td></tr>
<tr><td><label>SWIS ID</label></td><td>99881</td></tr>
<tr><td><label>Age</label></td><td>0</td></tr>
<tr><td><label>Gender</label></td><td>Unknown</td></tr>
<tr><td><label>Race</label></td><td>Unknown</td></tr>
<tr><td><label>Height</label></td><td>6 ft 0 in</td></tr>
<tr><td><label>Weight</label></td><td>160 lbs</td></tr>
<tr><td><label>Hair</label></td><td>Black</td></tr>
<tr><td><label>Eyes</label></td><td>Brown</td></tr>
<tr><td><label>Arresting Agency</label></td><td>Test Agency</td></tr>
<tr><td><label>Booking Date</label></td><td>11/22/2026 11:45 PM</td></tr>
<tr><td><label>Assigned Facility</label></td><td>Test Facility</td></tr>
<tr><td><label>Projected Release Date</label></td><td>Unknown</td></tr>
</table></div>
<div id="charge-info"><h3><span class="court-case-number">Court Case No. <b>00CR00001</b></span>
<span class="da-case-number">DA Case No. <b>100001</b></span>
<span class="citation-number">Citation No. <b>None</b></span></h3><div><ol>
<li><span class="charge-description-display">EXAMPLE CHARGE A</span><p>Bail: <span class="charge-bail-value">$0</span></p><p>Status: <span class="charge-status-value">Pending</span></p></li>
<li><span class="charge-description-display">EXAMPLE CHARGE B</span><p>Bail: <span class="charge-bail-value">$500</span></p><p>Status: <span class="charge-status-value">Pending</span></p></li>
</ol></div></div></html>'''
URL = "https://apps.mcso.us/PAID/Home/Booking?id=123456"


def test_full_fields():
    result = parse_full_record_html(SAMPLE, URL)
    assert result["booking_id"] == "123456"
    assert result["booking_detail"]["swis_id"] == "99881"
    assert result["booking_detail"]["arresting_agency"] == "Test Agency"
    assert result["booking_detail"]["age_displayed"] == "0"
    assert result["booking_detail"]["booking_datetime_local"] == "11/22/2026 11:45 PM"
    assert result["case_groups"][0]["court_case_number"] == "00CR00001"
    assert [c["bail_displayed"] for c in result["case_groups"][0]["charges"]] == ["$0", "$500"]
    assert result["charges"] == ["EXAMPLE CHARGE A", "EXAMPLE CHARGE B"]


def test_booking_id_mismatch():
    try:
        parse_full_record_html(SAMPLE.replace('value="123456"', 'value="123457"'), URL)
    except ValueError as error:
        assert "match" in str(error).lower()
    else:
        raise AssertionError("Mismatch must fail closed")
