from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import UnexpectedAlertPresentException
from bs4 import BeautifulSoup

options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome('chromedriver',options=options)

NovelName = ''
main_url = 'http://tw.zhsxs.com/'  

driver.get(main_url)

find = False

def searchBar(name):
    try:
        search = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, 'search_key'))
        )
        search.send_keys(NovelName)
        search.send_keys(Keys.RETURN)
    except TimeoutException as exception:
        print("Time out!!")

list_number = 0
count = 0

while find is False:

    if list_number < count and list_number > 0:
        num = 1
        soup = BeautifulSoup(driver.page_source)
        for title in soup.select('.td2 a'):
            if num == list_number:
                NovelName = title.text
            num += 1
        XPath = '//*[@id="newest"]/table[1]/tbody/tr[' + str((int(list_number)+1)) + ']/td[2]/a'
        driver.find_element(By.XPATH,XPath).click()
    else:
        NovelName = input("Please input the complete name of the novel:")
        searchBar(NovelName)

    try:
        button = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="novel_toolbar"]/a[1]'))
        )
        button.click()
        find = True
        break
    except TimeoutException as exception:
        pass

    try:
        button = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'td1'))
        )
        soup = BeautifulSoup(driver.page_source)
        print("List of " + NovelName)
        count = 1;
        for title in soup.select('.td2 a'):
            print(count,". ",title.text)
            count += 1
        list_number = int(input("Please input the number on the list to select the novel or it will keep searching:"))
    except TimeoutException as exception:
        print("Sorry! Can't find any novel!")      
        
filename = NovelName + ".txt"

button = WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.XPATH, '//*[@id="form1"]/div[2]/table[4]/tbody/tr[1]/td[1]/a'))
)
button.click()

first = True
end = False

while end is False:

    try:
        content = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="form1"]/table[2]/tbody/tr[1]/td/div[5]'))
        )
        if first is True:
            with open(filename, mode="w", encoding="utf-8") as file:
               file.write(content.text+"\n")
            first = False            
        else:
            with open(filename, mode="a", encoding="utf-8") as file:
                file.write(content.text+"\n")
            
        next = driver.find_element(By.XPATH, '//*[@id="form1"]/div[5]/a[3]')
        next.click()
    except UnexpectedAlertPresentException as exception:
        end = True

print("Progam End!")
driver.close()